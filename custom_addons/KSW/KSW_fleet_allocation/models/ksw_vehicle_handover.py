from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_fleet_request import VEHICLE_TYPES

# Employee details the form records. Copied onto the form when the employee
# is chosen, never computed: the paper is a record of that day, and a later
# change of phone or iqama must not rewrite a signed form.
_EMPLOYEE_SNAPSHOT = ('name', 'emp_no', 'iqama', 'phone')

HANDOVER_TYPES = [
    # The two boxes on the paper. "Handover" (تسليم): the user hands the
    # vehicle in (to the workshop); "Received" (استلام): the new user takes it.
    ('handover', 'Handover to Workshop'),
    ('receive', 'Received by New Driver'),
]

DOCUMENT_TYPES = [
    ('traffic_license', 'Traffic License'),
    ('insurance', 'Insurance Letter'),
    ('inspection_card', 'Periodic Inspection'),
    ('operation_card', 'Operation Card'),
    ('driver_card', 'Driver Card'),
    ('other', 'Other'),
]


class KswVehicleHandover(models.Model):
    """Form HR-12, «نموذج تسليم واستلام المركبة»: filled in Odoo, printed,
    signed on paper and filed back as a scan.

    Two separate processes, one form each (they are not chained):
    * Handover to Workshop: created when the GM sends a released vehicle to
      the workshop; the workshop receives it and fills the inspection page.
    * Received by New Driver: created when the GM transfers a vehicle to a
      department; that department fills the new driver's details.
    """
    _name = 'ksw.vehicle.handover'
    _description = 'Vehicle Handover Form (HR-12)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False, readonly=True)
    handover_type = fields.Selection(HANDOVER_TYPES, string='Request Type', required=True, readonly=True)
    state = fields.Selection([
        ('draft', 'Being Filled'),
        ('signed', 'Signed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True, copy=False, index=True)
    form_date = fields.Date(string='Date', required=True, default=fields.Date.context_today)

    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', required=True, readonly=True,
                                 index=True, ondelete='restrict')
    vehicle_code = fields.Char(related='vehicle_id.name', string='Code #')
    plate_number = fields.Char(related='vehicle_id.plate_number', string='Plate Number')
    vehicle_type = fields.Selection(related='vehicle_id.vehicle_type', string='Vehicle Type')
    department_id = fields.Many2one('hr.department', string='Requesting Department', readonly=True, index=True)
    workshop_request_id = fields.Many2one('ksw.workshop.request', string='Workshop Request',
                                          readonly=True, index=True, ondelete='set null')
    move_id = fields.Many2one('ksw.fleet.move', string='Transfer', readonly=True, index=True,
                              ondelete='set null')

    # Previous vehicle user / بيانات المستخدم القديم
    previous_employee_id = fields.Many2one('hr.employee', string='Previous User')
    previous_name = fields.Char(string='Previous User Name')
    previous_emp_no = fields.Char(string='Previous User Emp. No')
    previous_iqama = fields.Char(string='Previous User ID #')
    previous_phone = fields.Char(string='Previous User Phone')
    # New vehicle user / بيانات المستخدم الجديد
    new_employee_id = fields.Many2one('hr.employee', string='New User')
    new_name = fields.Char(string='New User Name')
    new_emp_no = fields.Char(string='New User Emp. No')
    new_iqama = fields.Char(string='New User ID #')
    new_phone = fields.Char(string='New User Phone')

    change_reason = fields.Selection([
        ('vacation', 'Vacation'),
        ('suspension', 'Suspension'),
        ('other', 'Other'),
    ], string='Reason for User Change')
    change_reason_note = fields.Char(string='Reason Details')
    workshop_reason = fields.Text(string='Reason to Hand Over to the Workshop')
    supervisor_id = fields.Many2one('hr.employee', string='Supervisor')

    # Administrative use only / خاص بقسم الشؤون الإدارية
    document_ids = fields.One2many('ksw.vehicle.handover.document', 'handover_id', string='Documents')
    new_position = fields.Char(string='Position')
    license_type = fields.Char(string='License Type')
    license_expiry = fields.Date(string='License Expiry')
    admin_remarks = fields.Text(string='Administrative Remarks')

    # Financial matters / متعلقات مالية
    financial_note_1 = fields.Char(string='Financial Matter 1')
    financial_note_2 = fields.Char(string='Financial Matter 2')
    financial_note_3 = fields.Char(string='Financial Matter 3')
    official_remarks = fields.Text(string='Executive Director Remarks')

    # Delegation process / إجراءات التفويض
    delegation_add_datetime = fields.Datetime(string='New User Added On')
    delegation_remove_datetime = fields.Datetime(string='Old User Removed On')
    delegation_remarks = fields.Text(string='Delegation Remarks')

    # Vehicle inspection form / نموذج فحص المركبة
    inspection_line_ids = fields.One2many('ksw.vehicle.handover.inspection', 'handover_id',
                                          string='Inspection')
    inspection_datetime = fields.Datetime(string='Inspected On')
    inspection_total = fields.Float(string='Total Value', compute='_compute_inspection_total', store=True)
    receiver_name = fields.Char(string='Vehicle Receiver')
    deliverer_name = fields.Char(string='Vehicle Handed Over By')
    workshop_official_name = fields.Char(string='Workshop Official')

    signed_attachment_ids = fields.Many2many(
        'ir.attachment', 'ksw_vehicle_handover_signed_rel', 'handover_id', 'attachment_id',
        string='Signed Copy', copy=False,
        help='The printed form, signed on paper and scanned back.')
    signed_date = fields.Datetime(readonly=True, copy=False)
    signed_by_id = fields.Many2one('res.users', string='Filed By', readonly=True, copy=False)

    @api.depends('inspection_line_ids.value')
    def _compute_inspection_total(self):
        for form in self:
            form.inspection_total = sum(form.inspection_line_ids.mapped('value'))

    # ------------------------------------------------------------------
    # Employee snapshot
    # ------------------------------------------------------------------
    @api.model
    def _employee_snapshot(self, employee, prefix):
        employee = employee.sudo()
        if not employee:
            return {f'{prefix}_{key}': False for key in _EMPLOYEE_SNAPSHOT}
        # x_employee_no comes from KSW_payroll; this module does not depend on it.
        emp_no = employee['x_employee_no'] if 'x_employee_no' in employee._fields else False
        return {
            f'{prefix}_name': employee.name,
            f'{prefix}_emp_no': emp_no or employee.barcode or False,
            f'{prefix}_iqama': employee.identification_id or False,
            f'{prefix}_phone': employee.mobile_phone or employee.work_phone or False,
        }

    @api.onchange('previous_employee_id')
    def _onchange_previous_employee_id(self):
        self.update(self._employee_snapshot(self.previous_employee_id, 'previous'))

    @api.onchange('new_employee_id')
    def _onchange_new_employee_id(self):
        self.update(self._employee_snapshot(self.new_employee_id, 'new'))
        employee = self.new_employee_id.sudo()
        if employee and not self.new_position:
            self.new_position = employee.job_title or employee.job_id.name

    def _fill_snapshots(self, vals):
        for prefix in ('previous', 'new'):
            field = f'{prefix}_employee_id'
            if field in vals and f'{prefix}_name' not in vals:
                vals.update(self._employee_snapshot(self.env['hr.employee'].browse(vals[field]), prefix))
        return vals

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('ksw.vehicle.handover') or 'New'
            self._fill_snapshots(vals)
            if not vals.get('document_ids'):
                vals['document_ids'] = [(0, 0, {'sequence': i, 'document_type': key})
                                        for i, (key, _label) in enumerate(DOCUMENT_TYPES)]
        forms = super().create(vals_list)
        for form in forms.filtered(lambda f: not f.inspection_line_ids):
            form._ksw_load_inspection_items()
        return forms

    def write(self, vals):
        if not self.env.su and self.filtered(lambda f: f.state != 'draft') \
                and set(vals) - {'message_main_attachment_id'}:
            raise UserError(_('A signed or cancelled handover form cannot be changed.'))
        return super().write(self._fill_snapshots(dict(vals)))

    def unlink(self):
        if not self.env.su:
            raise UserError(_('A handover form is cancelled, never deleted.'))
        return super().unlink()

    def _check_lines_editable(self):
        if not self.env.su and self.filtered(lambda f: f.state != 'draft'):
            raise UserError(_('A signed or cancelled handover form cannot be changed.'))

    def _ksw_load_inspection_items(self, source=None):
        """The checklist for this vehicle's type, or a copy of `source`'s
        inspection (the workshop's, when the vehicle comes out of holding)."""
        self.ensure_one()
        if source and source.inspection_line_ids:
            commands = [(0, 0, {
                'sequence': line.sequence, 'item_id': line.item_id.id, 'name': line.name,
                'depreciation': line.depreciation, 'value': line.value, 'remark': line.remark,
            }) for line in source.inspection_line_ids]
        else:
            items = self.env['ksw.vehicle.inspection.item'].search([
                '|', ('vehicle_type', '=', False), ('vehicle_type', '=', self.vehicle_id.vehicle_type)])
            commands = [(0, 0, {'sequence': item.sequence, 'item_id': item.id, 'name': item.name})
                        for item in items]
        self.sudo().write({'inspection_line_ids': commands})

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_print(self):
        return self.env.ref('KSW_fleet_allocation.action_report_vehicle_handover').report_action(self)

    def action_open_workshop_request(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'ksw.workshop.request',
                'res_id': self.workshop_request_id.id, 'view_mode': 'form'}

    def action_mark_signed(self):
        """File the signed paper back: the scan is required."""
        for form in self:
            if form.state != 'draft':
                raise UserError(_('%s is not being filled.', form.name))
            if not form.signed_attachment_ids:
                raise UserError(_('Attach the signed and scanned form first (Signed Copy).'))
            form._check_complete()
        self.write({'state': 'signed', 'signed_date': fields.Datetime.now(),
                    'signed_by_id': self.env.user.id})
        for form in self:
            # The scan belongs to the form, so it opens from its chatter too.
            form.signed_attachment_ids.sudo().write({'res_model': form._name, 'res_id': form.id})
            form.message_post(body=Markup(_('Signed copy filed by %s.')) % self.env.user.name,
                              subtype_xmlid='mail.mt_note')
        return True

    def _check_complete(self):
        """What the paper cannot go without, per process."""
        self.ensure_one()
        if self.handover_type == 'handover':
            if not self.inspection_datetime:
                raise ValidationError(_('Fill in when the workshop inspected the vehicle.'))
        elif not self.new_employee_id:
            raise ValidationError(_('Choose the new user of the vehicle.'))

    def action_reset_draft(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only the system administrator reopens a signed form.'))
        self.sudo().write({'state': 'draft', 'signed_date': False, 'signed_by_id': False})
        return True

    def _ksw_cancel(self, reason):
        for form in self.sudo().filtered(lambda f: f.state == 'draft'):
            form.state = 'cancelled'
            form.message_post(body=reason, subtype_xmlid='mail.mt_note')


class KswVehicleHandoverDocument(models.Model):
    _name = 'ksw.vehicle.handover.document'
    _description = 'Vehicle Handover Form: Document'
    _order = 'sequence, id'

    handover_id = fields.Many2one('ksw.vehicle.handover', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    document_type = fields.Selection(DOCUMENT_TYPES, string='Type', required=True)
    present = fields.Boolean(string='Handed Over')
    remark = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.handover_id._check_lines_editable()
        return lines

    def write(self, vals):
        self.handover_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        self.handover_id._check_lines_editable()
        return super().unlink()


class KswVehicleHandoverInspection(models.Model):
    _name = 'ksw.vehicle.handover.inspection'
    _description = 'Vehicle Handover Form: Inspection Item'
    _order = 'sequence, id'

    handover_id = fields.Many2one('ksw.vehicle.handover', required=True, index=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    item_id = fields.Many2one('ksw.vehicle.inspection.item', string='Checklist Item', ondelete='set null')
    name = fields.Char(string='Item', required=True)
    depreciation = fields.Float(string='Depreciation %')
    value = fields.Float(string='Value')
    remark = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.handover_id._check_lines_editable()
        return lines

    def write(self, vals):
        self.handover_id._check_lines_editable()
        return super().write(vals)

    def unlink(self):
        self.handover_id._check_lines_editable()
        return super().unlink()

    @api.constrains('depreciation')
    def _check_depreciation(self):
        for line in self:
            if not 0 <= line.depreciation <= 100:
                raise ValidationError(_('Depreciation is a percentage between 0 and 100.'))


class KswVehicleInspectionItem(models.Model):
    """The inspection checklist, per vehicle type: what the workshop checks
    on handover. Configuration, not code: the paper's list is tanker-shaped
    (water tank, water motor), an Isuzu or a trailer head needs its own."""
    _name = 'ksw.vehicle.inspection.item'
    _description = 'Vehicle Inspection Checklist Item'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    vehicle_type = fields.Selection(VEHICLE_TYPES, string='Vehicle Type',
                                    help='Empty: checked on every vehicle type.')
    active = fields.Boolean(default=True)
