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
    # The contact too: people know a client by its Odoo name, which often
    # differs from the BAS account's (a test or renamed contact).
    _rec_names_search = ['name', 'bas_code', 'partner_id']

    # The client is the customer CONTACT (Oct 2026): one record per contact,
    # whether or not it has a BAS account. The BAS account, when there is
    # one, is extra information on the same record -- where the opening value
    # came from and what BAS says today. A BAS account nobody has linked to a
    # contact keeps a record of its own until `_link_contacts` finds one.
    partner_id = fields.Many2one(
        'res.partner', string='Contact', index=True, ondelete='restrict', readonly=True)
    customer_id = fields.Many2one(
        'ksw.bas.customer', string='BAS Customer',
        ondelete='restrict', index=True, readonly=True)
    bas_code = fields.Char(string='Account Code', related='customer_id.bas_code',
                           store=True, index=True)
    name = fields.Char(string='Client', compute='_compute_name', store=True)
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
    _client_identified = models.Constraint(
        'CHECK (customer_id IS NOT NULL OR partner_id IS NOT NULL)',
        'A client multiplier needs a contact or a BAS customer.')
    _partner_uniq = models.Constraint(
        'UNIQUE (partner_id)', 'This contact already has a multiplier record.')

    @api.depends('partner_id.name', 'customer_id.name_ar', 'customer_id.name_en',
                 'customer_id.bas_code')
    def _compute_name(self):
        for rec in self:
            c = rec.customer_id
            rec.name = (rec.partner_id.name or c.name_ar or c.name_en
                        or c.bas_code or '').strip()

    @api.depends('name', 'bas_code')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '[%s] %s' % (rec.bas_code, rec.name) if rec.bas_code \
                else (rec.name or '')

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
    def _multiplier_for_partners(self, partners, day):
        """{partner id: multiplier in force on ``day``}, for Odoo-side callers.

        Same rule as `_multiplier_on`: a contact with no client, or no value
        on that day, is absent -- "not recorded", never zero.
        """
        day = fields.Date.to_date(day)
        partners = partners.commercial_partner_id
        result = {}
        for client in self.sudo().search([('partner_id', 'in', partners.ids)]):
            rate = client._rate_on(day)
            if rate and client.partner_id.id not in result:
                result[client.partner_id.id] = rate.multiplier
        return result

    @api.model
    def _ensure_for_partners(self, partners):
        """Give every customer contact its own client.

        A contact whose BAS account already has a client (not yet linked to
        any contact) takes that one, with BAS's value. Any other customer
        starts at 1; if its BAS account turns up later, `_adopt_odoo_clients`
        attaches it and swaps an untouched 1 for BAS's value.
        """
        partners = partners.sudo().filtered(
            lambda p: p.active and not p.parent_id and p.customer_rank > 0
            and self._is_client_contact(p))
        partners -= self.sudo().search([('partner_id', 'in', partners.ids)]).partner_id
        if not partners:
            return self.browse()
        self._link_contacts(partners)
        todo = partners - self.sudo().search([('partner_id', 'in', partners.ids)]).partner_id
        if not todo:
            return self.browse()
        clients = self.sudo().create([{'partner_id': p.id} for p in todo])
        self.env['ksw.client.multiplier.rate'].sudo().create([
            {'client_id': c.id, 'multiplier': 1.0, 'source': 'default'} for c in clients])
        return clients

    @api.model
    def _is_client_contact(self, partner):
        """Not one of the companies' own contacts, and not a BAS ledger
        account that only became a contact through the GL import (a cash till
        1201, employee debt 1207, expense account 31xx...): a contact with a
        BAS account number is a client only on a 1203 customer account."""
        if partner in self.env['res.company'].sudo().search([]).partner_id:
            return False
        codes = [(partner[f] or '').strip() for f in ('x_client_account_number', 'x_bas_code')
                 if f in partner._fields]
        codes = [c for c in codes if c]
        return not codes or any(c.startswith('1203') for c in codes)

    @api.model
    def _contact_for_codes(self, partners=None):
        """{BAS account code: contact}, from the account numbers on contacts."""
        Partner = self.env['res.partner'].sudo()
        code_fields = [f for f in ('x_client_account_number', 'x_bas_code')
                       if f in Partner._fields]
        result = {}
        for f in code_fields:
            domain = [(f, '!=', False), ('active', '=', True)]
            if partners is not None:
                domain.append(('id', 'in', partners.ids))
            # Top-level contacts first, so a site address never wins.
            for p in Partner.search(domain, order='parent_id desc, id desc'):
                result[(p[f] or '').strip()] = p.commercial_partner_id
        return result

    @api.model
    def _link_contacts(self, partners=None):
        """Put the contact on BAS clients that have none.

        Found through the BAS mirror's own link, else the account number on
        the contact. A contact already holding a client is not given a second
        one: if that client is only the automatic default it makes way,
        otherwise the BAS client stays unlinked and the clash is logged.
        """
        clients = self.sudo().search([('customer_id', '!=', False), ('partner_id', '=', False)])
        if not clients:
            return 0
        by_code = self._contact_for_codes(partners)
        linked = 0
        for client in clients.sorted('bas_code'):
            partner = client.customer_id.partner_id.commercial_partner_id \
                or by_code.get(client.bas_code)
            if not partner or (partners is not None and partner not in partners):
                continue
            holder = self.sudo().search([('partner_id', '=', partner.id)])
            if holder:
                if not holder.customer_id and all(
                        r.source == 'default' for r in holder.rate_ids) \
                        and not holder.request_ids:
                    holder.unlink()
                else:
                    _logger.warning(
                        'Client multipliers: %s already has a client; BAS account %s '
                        'left unlinked.', partner.display_name, client.bas_code)
                    continue
            client.partner_id = partner
            linked += 1
        return linked

    @api.model
    def _backfill_existing_customers(self):
        """Give every existing customer with no client a multiplier of 1.

        Decided with the user on 2026-10-28: customers that predate the
        automatic default start at 1 too. That includes BAS "H" / "A" customer
        accounts (1203*) the BAS mirror does not sync, and if the mirror ever
        brings one in, `_adopt_odoo_clients` replaces the untouched 1 with
        BAS's value. Left out: the companies' own contacts, and BAS ledger
        accounts that only became contacts through the GL import -- cash
        tills (1201), employee debts (1207), other debtors (1208), expense
        and purchase accounts -- which are not clients.
        """
        Partner = self.env['res.partner'].sudo()
        partners = Partner.search([
            ('active', '=', True), ('parent_id', '=', False), ('customer_rank', '>', 0)])
        self._link_contacts()
        clients = self._ensure_for_partners(partners)
        _logger.info('Client multipliers: %s existing customers defaulted to 1.', len(clients))
        return clients

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
        missing = customers.filtered(lambda c: c.id not in existing)
        adopted = self._adopt_odoo_clients(missing)
        new_clients = self.sudo().create([
            {'customer_id': c.id} for c in missing - adopted])
        self._link_contacts()
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

    @api.model
    def _adopt_odoo_clients(self, customers):
        """BAS customers whose contact already has an Odoo-only client.

        That record takes the BAS account rather than a second client being
        opened beside it. If all it holds is the automatic default of 1, the
        default goes, so BAS's own value becomes the opening one; anything
        decided in Odoo (an approved request) is kept.
        """
        Partner = self.env['res.partner'].sudo()
        orphans = self.sudo().search([('customer_id', '=', False)])
        if not orphans:
            return customers.browse()
        by_partner = {c.partner_id.id: c for c in orphans}
        code_fields = [f for f in ('x_client_account_number', 'x_bas_code')
                       if f in Partner._fields]
        adopted = customers.browse()
        for customer in customers:
            candidates = customer.partner_id
            for f in code_fields:
                candidates |= Partner.search([(f, '=', customer.bas_code)])
            client = next((by_partner[p.id] for p in candidates if p.id in by_partner), None)
            if not client:
                continue
            defaults = client.rate_ids.filtered(lambda r: r.source == 'default')
            if defaults and defaults == client.rate_ids:
                defaults.unlink()
            client.customer_id = customer
            del by_partner[client.partner_id.id]
            adopted |= customer
        return adopted


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
        ('default', 'Default for a new customer'),
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
