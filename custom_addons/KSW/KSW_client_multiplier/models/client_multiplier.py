"""«الرد المضاعف»: each client's distance multiplier, owned by Odoo.

In BAS it is ``cod10.FACTORE``, one live value on the customer card: change
it and every past load of that customer silently changes with it, paid months
included. Here it is a **dated history** instead. A value applies from its
``date_from`` until the next one, so a change reaches only the loads on or
after its effective date.

Nobody writes to these records directly. Every user reads them; the only
way a value changes is an approved ``ksw.client.multiplier.request``
(``_apply_request``, run as sudo after the GM check). The ACL grants
write to the system administrator only, for repairs.

BAS is still where clients are created during the transfer, so the daily
refresh gives a new BAS customer its opening value from ``FACTORE``
(create-only: a client that has any history is never touched again) and
re-reads BAS's current value into ``bas_factor`` so the two can be compared.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class KswClientMultiplier(models.Model):
    _name = 'ksw.client.multiplier'
    _description = 'Client Multiplier'
    _inherit = ['mail.thread']
    _order = 'name, bas_code'
    _rec_names_search = ['name', 'bas_code']

    customer_id = fields.Many2one(
        'ksw.bas.customer', string='BAS Customer', required=True,
        ondelete='restrict', index=True, readonly=True)
    bas_code = fields.Char(string='Account Code', related='customer_id.bas_code',
                           store=True, index=True)
    name = fields.Char(string='Client', compute='_compute_name', store=True)
    partner_id = fields.Many2one(related='customer_id.partner_id', string='Contact')
    is_stopped = fields.Boolean(related='customer_id.is_stopped', string='Stopped in BAS')

    rate_ids = fields.One2many('ksw.client.multiplier.rate', 'client_id', string='History')
    request_ids = fields.One2many('ksw.client.multiplier.request', 'client_id', string='Requests')

    # Stored so the list can sort and group by it. The date moving on is not
    # a dependency the ORM can see, so the daily cron recomputes it as well.
    current_multiplier = fields.Float(
        string='Multiplier', compute='_compute_current', store=True, digits=(16, 2),
        help='«الرد المضاعف» in force today.')
    current_since = fields.Date(string='In Force Since', compute='_compute_current', store=True)
    next_multiplier = fields.Float(string='Next Multiplier', compute='_compute_current',
                                   store=True, digits=(16, 2))
    next_date = fields.Date(string='Next Change On', compute='_compute_current', store=True)
    pending_request_id = fields.Many2one(
        'ksw.client.multiplier.request', string='Pending Request',
        compute='_compute_pending_request')

    bas_factor = fields.Float(
        string='BAS Value', readonly=True, digits=(16, 2),
        help='cod10.FACTORE as BAS holds it now. Shown only to confirm the '
             'transfer: Odoo does not read it after the opening value.')
    bas_checked_on = fields.Datetime(string='BAS Checked On', readonly=True)
    differs_from_bas = fields.Boolean(
        string='Differs from BAS', compute='_compute_differs', store=True)

    _customer_uniq = models.Constraint(
        'UNIQUE (customer_id)', 'This BAS customer already has a multiplier record.')

    @api.depends('customer_id.name_ar', 'customer_id.name_en', 'customer_id.bas_code')
    def _compute_name(self):
        for rec in self:
            c = rec.customer_id
            rec.name = (c.name_ar or c.name_en or c.bas_code or '').strip()

    @api.depends('rate_ids.date_from', 'rate_ids.multiplier')
    def _compute_current(self):
        today = fields.Date.context_today(self)
        for rec in self:
            current = rec._rate_on(today)
            later = rec.rate_ids.filtered(
                lambda r: r.date_from and r.date_from > today).sorted('date_from')[:1]
            rec.current_multiplier = current.multiplier if current else 0.0
            rec.current_since = current.date_from if current else False
            rec.next_multiplier = later.multiplier
            rec.next_date = later.date_from

    @api.depends('current_multiplier', 'bas_factor', 'bas_checked_on')
    def _compute_differs(self):
        for rec in self:
            rec.differs_from_bas = bool(rec.bas_checked_on) and \
                abs(rec.current_multiplier - rec.bas_factor) > 0.0001

    def _compute_pending_request(self):
        Request = self.env['ksw.client.multiplier.request'].sudo()
        pending = Request.search([('client_id', 'in', self.ids), ('state', '=', 'pending')])
        by_client = {r.client_id.id: r for r in pending}
        for rec in self:
            rec.pending_request_id = by_client.get(rec.id, False)

    # ------------------------------------------------------------------
    # The resolver
    # ------------------------------------------------------------------
    def _rate_on(self, day):
        """The history line in force on ``day`` (the opening line has no date)."""
        self.ensure_one()
        lines = self.rate_ids.filtered(lambda r: not r.date_from or r.date_from <= day)
        return lines.sorted(lambda r: (r.date_from or fields.Date.to_date('1900-01-01'), r.id))[-1:]

    @api.model
    def _multiplier_on(self, bas_codes, day):
        """{bas_code: multiplier in force on ``day``} — what every consumer reads.

        A code Odoo has no client for, or no value on that day, is absent:
        the caller must treat it as "not recorded", never as zero.
        """
        day = fields.Date.to_date(day)
        clients = self.sudo().search([('bas_code', 'in', list(set(bas_codes)))])
        result = {}
        for client in clients:
            rate = client._rate_on(day)
            if rate:
                result[client.bas_code] = rate.multiplier
        return result

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_request_change(self):
        self.ensure_one()
        if self.pending_request_id:
            return self.pending_request_id._open()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Request a Multiplier Change'),
            'res_model': 'ksw.client.multiplier.request',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_client_id': self.id},
        }

    def action_refresh_from_bas(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only the system administrator can refresh from BAS.'))
        stats = self.sudo()._refresh_from_bas()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success', 'sticky': False,
                'message': _('%(new)s clients added, %(checked)s compared with BAS, '
                             '%(differ)s differ.', **stats),
            },
        }

    @api.model
    def _cron_daily_refresh(self):
        try:
            self._refresh_from_bas()
        except Exception:
            # BAS down must not stop "today's value becomes current".
            _logger.exception('Client multipliers: BAS refresh failed')
        self.search([])._compute_current()

    @api.model
    def _refresh_from_bas(self):
        """Create a client for every BAS customer, seed opening values, re-read BAS."""
        Customer = self.env['ksw.bas.customer'].sudo()
        customers = Customer.search([])
        existing = {c.customer_id.id: c for c in self.sudo().search([])}
        new_clients = self.sudo().create([
            {'customer_id': c.id} for c in customers if c.id not in existing])
        clients = self.sudo().search([])

        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute("SELECT RTRIM(DCODE1) code, FACTORE FROM cod10 "
                        "WHERE DCODE1 LIKE '1203%'")
            bas = {r['code']: float(r['FACTORE'] or 0.0) for r in cur.fetchall()}
        finally:
            conn.close()

        now = fields.Datetime.now()
        Rate = self.env['ksw.client.multiplier.rate'].sudo()
        seeds = []
        for client in clients:
            value = bas.get(client.bas_code)
            if value is None:
                continue
            client.write({'bas_factor': value, 'bas_checked_on': now})
            # Opening value: only for a client with no history at all, so a
            # BAS edit can never overwrite what Odoo has decided.
            if not client.rate_ids and value:
                seeds.append({'client_id': client.id, 'multiplier': value,
                              'source': 'opening'})
        if seeds:
            Rate.create(seeds)
        return {'new': len(new_clients), 'checked': len(bas),
                'differ': len(clients.filtered('differs_from_bas'))}


class KswClientMultiplierRate(models.Model):
    _name = 'ksw.client.multiplier.rate'
    _description = 'Client Multiplier History'
    _order = 'client_id, date_from desc, id desc'

    client_id = fields.Many2one('ksw.client.multiplier', required=True,
                                ondelete='cascade', index=True)
    date_from = fields.Date(
        string='Effective From',
        help='Empty on the opening value taken from BAS: it applies to everything before '
             'the first approved change.')
    multiplier = fields.Float(string='Multiplier', required=True, digits=(16, 2))
    source = fields.Selection([
        ('opening', 'Opening value from BAS'),
        ('request', 'Approved request'),
    ], string='Source', required=True, default='request')
    request_id = fields.Many2one('ksw.client.multiplier.request', string='Request',
                                 ondelete='restrict')
    approved_by_id = fields.Many2one(related='request_id.approved_by_id')

    _client_date_uniq = models.Constraint(
        'UNIQUE (client_id, date_from)',
        'A client can have only one multiplier per effective date.')
    _positive = models.Constraint('CHECK (multiplier > 0)', 'The multiplier must be positive.')

    # UNIQUE treats NULLs as distinct (and NULLS NOT DISTINCT needs PG 15),
    # so "one opening value per client" is checked here.
    @api.constrains('client_id', 'date_from')
    def _check_one_opening(self):
        for rec in self.filtered(lambda r: not r.date_from):
            if len(rec.client_id.rate_ids.filtered(lambda r: not r.date_from)) > 1:
                raise ValidationError(_('A client can have only one opening value.'))

    @api.depends('date_from', 'multiplier')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s (%s)' % (
                rec.multiplier, rec.date_from or _('opening'))
