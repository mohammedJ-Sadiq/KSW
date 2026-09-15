from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

WARRANTY_WARNING_DAYS = 30


class ItAsset(models.Model):
    _name = 'it.asset'
    _description = 'IT Asset'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'
    _rec_names_search = ['name', 'asset_tag', 'serial_number']

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    asset_tag = fields.Char(
        string='Asset Tag', required=True, copy=False, readonly=True,
        default='New', tracking=True,
    )
    name = fields.Char(
        string='Asset Name', required=True, tracking=True,
        help="Short description, e.g. 'Dell Latitude 5420 Laptop'.",
    )
    category_id = fields.Many2one(
        'it.asset.category', string='Category', required=True, tracking=True,
    )
    brand = fields.Char()
    model_name = fields.Char(string='Model')
    serial_number = fields.Char(string='Serial / IMEI', tracking=True)
    image_1920 = fields.Image(max_width=1920, max_height=1920)
    image_128 = fields.Image(
        string='Photo (128)', related='image_1920', max_width=128, max_height=128, store=True,
    )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    state = fields.Selection([
        ('available', 'Available'),
        ('assigned', 'Assigned'),
        ('maintenance', 'In Maintenance'),
        ('retired', 'Retired'),
        ('lost', 'Lost / Stolen'),
    ], default='available', required=True, tracking=True)
    active = fields.Boolean(default=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Assigned To', tracking=True, readonly=True, copy=False,
    )
    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )

    # ------------------------------------------------------------------
    # Procurement / warranty
    # ------------------------------------------------------------------
    location = fields.Char(help="Physical location, e.g. 'HQ - 3rd Floor Store'.")
    supplier_id = fields.Many2one('res.partner', string='Vendor')
    purchase_date = fields.Date()
    currency_id = fields.Many2one(related='company_id.currency_id')
    purchase_value = fields.Monetary(currency_field='currency_id')
    warranty_expiry_date = fields.Date(tracking=True)
    warranty_status = fields.Selection([
        ('none', 'No Warranty'),
        ('valid', 'Valid'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
    ], compute='_compute_warranty_status', search='_search_warranty_status')
    notes = fields.Text()
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    color = fields.Integer(related='category_id.color', store=True, readonly=True)

    # ------------------------------------------------------------------
    # Related records
    # ------------------------------------------------------------------
    assignment_ids = fields.One2many('it.asset.assignment', 'asset_id', string='Assignment History')
    assignment_count = fields.Integer(compute='_compute_assignment_count')
    maintenance_ids = fields.One2many('it.asset.maintenance', 'asset_id', string='Maintenance History')
    maintenance_count = fields.Integer(compute='_compute_maintenance_count')

    _asset_tag_uniq = models.Constraint(
        'unique(asset_tag)',
        'This asset tag already exists.',
    )

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _compute_display_name(self):
        for asset in self:
            asset.display_name = f'[{asset.asset_tag}] {asset.name}'

    def _compute_assignment_count(self):
        counts = self.env['it.asset.assignment']._read_group(
            [('asset_id', 'in', self.ids)], ['asset_id'], ['__count'],
        )
        count_map = {asset.id: count for asset, count in counts}
        for asset in self:
            asset.assignment_count = count_map.get(asset.id, 0)

    def _compute_maintenance_count(self):
        counts = self.env['it.asset.maintenance']._read_group(
            [('asset_id', 'in', self.ids)], ['asset_id'], ['__count'],
        )
        count_map = {asset.id: count for asset, count in counts}
        for asset in self:
            asset.maintenance_count = count_map.get(asset.id, 0)

    @api.depends('warranty_expiry_date')
    def _compute_warranty_status(self):
        today = fields.Date.context_today(self)
        warn_before = today + relativedelta(days=WARRANTY_WARNING_DAYS)
        for asset in self:
            if not asset.warranty_expiry_date:
                asset.warranty_status = 'none'
            elif asset.warranty_expiry_date < today:
                asset.warranty_status = 'expired'
            elif asset.warranty_expiry_date <= warn_before:
                asset.warranty_status = 'expiring'
            else:
                asset.warranty_status = 'valid'

    def _search_warranty_status(self, operator, value):
        today = fields.Date.context_today(self)
        warn_before = today + relativedelta(days=WARRANTY_WARNING_DAYS)
        wanted = list(value) if isinstance(value, (list, tuple, set)) else [value]
        domains = []
        if 'none' in wanted:
            domains.append([('warranty_expiry_date', '=', False)])
        if 'expired' in wanted:
            domains.append([('warranty_expiry_date', '<', today)])
        if 'expiring' in wanted:
            domains.append([('warranty_expiry_date', '>=', today), ('warranty_expiry_date', '<=', warn_before)])
        if 'valid' in wanted:
            domains.append([('warranty_expiry_date', '>', warn_before)])
        if not domains:
            return [('id', '=', False)]
        domain = domains[0]
        for extra in domains[1:]:
            domain = ['|'] + domain + extra
        if operator in ('not in', '!='):
            return ['!'] + domain
        return domain

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('asset_tag', 'New') == 'New':
                vals['asset_tag'] = self.env['ir.sequence'].next_by_code('it.asset') or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_open_assign_wizard(self):
        self.ensure_one()
        if self.state != 'available':
            raise UserError(_('Only an available asset can be assigned. Return or repair it first.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Assign Asset'),
            'res_model': 'it.asset.assign.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_open_return_wizard(self):
        self.ensure_one()
        if self.state != 'assigned':
            raise UserError(_('This asset is not currently assigned.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Return Asset'),
            'res_model': 'it.asset.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_asset_id': self.id},
        }

    def action_send_maintenance(self):
        for asset in self:
            if asset.state != 'available':
                raise UserError(_('Only an available asset can be sent to maintenance.'))
            self.env['it.asset.maintenance'].create({
                'asset_id': asset.id,
                'issue': _('Maintenance'),
            })
            asset.state = 'maintenance'

    def action_return_from_maintenance(self):
        for asset in self:
            if asset.state != 'maintenance':
                raise UserError(_('This asset is not in maintenance.'))
            open_maintenance = asset.maintenance_ids.filtered(lambda m: m.state == 'in_progress')
            open_maintenance.write({
                'state': 'done',
                'date_end': fields.Date.context_today(self),
            })
            asset.state = 'available'

    def action_mark_lost(self):
        for asset in self:
            asset.write({'state': 'lost'})

    def action_retire(self):
        for asset in self:
            if asset.state not in ('available', 'lost'):
                raise UserError(_('Return or repair this asset before retiring it.'))
            asset.write({'state': 'retired', 'active': False})

    def action_reactivate(self):
        self.write({'state': 'available', 'active': True})

    def action_view_assignments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Assignment History'),
            'res_model': 'it.asset.assignment',
            'view_mode': 'list,form',
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    def action_view_maintenance(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Maintenance History'),
            'res_model': 'it.asset.maintenance',
            'view_mode': 'list,form',
            'domain': [('asset_id', '=', self.id)],
            'context': {'default_asset_id': self.id},
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    def _cron_warranty_expiry_reminder(self):
        target_date = fields.Date.context_today(self) + relativedelta(days=WARRANTY_WARNING_DAYS)
        assets = self.search([('warranty_expiry_date', '=', target_date), ('active', '=', True)])
        it_group = self.env.ref('KSW_helpdesk.group_helpdesk_agent', raise_if_not_found=False)
        users = it_group.users if it_group else self.env['res.users']
        for asset in assets:
            for user in users:
                asset.activity_schedule(
                    'mail.mail_activity_data_todo',
                    summary=_('Warranty expiring in %d days', WARRANTY_WARNING_DAYS),
                    note=_(
                        'Warranty for %(asset)s (%(tag)s) expires on %(date)s.',
                        asset=asset.name, tag=asset.asset_tag, date=asset.warranty_expiry_date,
                    ),
                    user_id=user.id,
                )
