from odoo import _, api, fields, models
from odoo.exceptions import UserError


def _job_warning(release_lines, addition_line):
    other = release_lines.sudo().filtered(lambda r: r.job_id != addition_line.sudo().job_id)
    if not addition_line or not other:
        return False
    return _('%(e)s: not %(j)s.',
             e=', '.join(other.employee_id.mapped('name')), j=addition_line.sudo().job_id.name)


class KswEmployeeReleaseDecisionWizard(models.TransientModel):
    """The GM, from the released employees: give them to a request, hold,
    end their service, or refuse the release."""
    _name = 'ksw.employee.release.decision.wizard'
    _description = 'Decide Released Employees'

    release_line_ids = fields.Many2many('ksw.employee.release.line', 'ksw_emp_rel_dec_line_rel', 'wizard_id',
                                        'line_id', string='Employees', required=True)
    decision = fields.Selection([
        ('transfer', 'Transfer to a Request'),
        ('hold', 'On Hold (stays, offered to the next request)'),
        ('terminate', 'End of Service'),
        ('keep', 'Refuse the Release (stays)'),
    ], required=True, default='transfer')
    demand_line_ids = fields.Many2many('ksw.employee.addition.line', 'ksw_emp_rel_dec_demand_rel', 'wizard_id',
                                       'line_id', compute='_compute_demand_line_ids')
    addition_line_id = fields.Many2one('ksw.employee.addition.line', string='For Request',
                                       domain="[('id', 'in', demand_line_ids)]")
    effective_date = fields.Date(default=fields.Date.context_today,
                                 help='Transfer: the day he starts in the new department. '
                                      'End of service: his termination date.')
    note = fields.Text(string='Note')
    job_warning = fields.Char(compute='_compute_job_warning')

    @api.depends('release_line_ids')
    def _compute_demand_line_ids(self):
        Line = self.env['ksw.employee.addition.line'].sudo()
        for wizard in self:
            departments = wizard.release_line_ids.sudo().department_id
            wizard.demand_line_ids = Line.search([
                ('status', 'in', ('gm', 'hold')), ('department_id', 'not in', departments.ids)])

    @api.depends('release_line_ids', 'addition_line_id', 'decision')
    def _compute_job_warning(self):
        for wizard in self:
            wizard.job_warning = wizard.decision == 'transfer' and _job_warning(
                wizard.release_line_ids, wizard.addition_line_id)

    def action_confirm(self):
        self.ensure_one()
        self.release_line_ids._ksw_decide(
            self.decision, addition_line=self.addition_line_id if self.decision == 'transfer' else False,
            effective_date=self.effective_date, note=self.note)
        return {'type': 'ir.actions.act_window_close'}


class KswEmployeeAdditionDecisionWizard(models.TransientModel):
    """The GM, from a department's request: fill it from released employees,
    hire from outside, hold, or refuse."""
    _name = 'ksw.employee.addition.decision.wizard'
    _description = 'Decide a Request for Employees'

    addition_line_id = fields.Many2one('ksw.employee.addition.line', required=True, readonly=True)
    open_quantity = fields.Integer(related='addition_line_id.open_quantity')
    decision = fields.Selection([
        ('fill', 'Give Released Employees'),
        ('external', 'Hire from Outside'),
        ('hold', 'On Hold'),
        ('refuse', 'Refuse'),
    ], required=True, default='fill')
    candidate_ids = fields.Many2many('ksw.employee.release.line', 'ksw_emp_add_dec_cand_rel', 'wizard_id',
                                     'line_id', compute='_compute_candidate_ids')
    release_line_ids = fields.Many2many('ksw.employee.release.line', 'ksw_emp_add_dec_line_rel', 'wizard_id',
                                        'line_id', string='Released Employees',
                                        domain="[('id', 'in', candidate_ids)]")
    quantity = fields.Integer(default=1)
    effective_date = fields.Date(default=fields.Date.context_today)
    note = fields.Text(string='Note')
    job_warning = fields.Char(compute='_compute_job_warning')

    @api.depends('addition_line_id')
    def _compute_candidate_ids(self):
        Release = self.env['ksw.employee.release.line'].sudo()
        for wizard in self:
            wizard.candidate_ids = Release.search([
                ('status', 'in', ('gm', 'hold')),
                ('department_id', '!=', wizard.addition_line_id.sudo().department_id.id)])

    @api.depends('release_line_ids', 'addition_line_id', 'decision')
    def _compute_job_warning(self):
        for wizard in self:
            wizard.job_warning = wizard.decision == 'fill' and _job_warning(
                wizard.release_line_ids, wizard.addition_line_id)

    def action_confirm(self):
        self.ensure_one()
        if self.decision == 'fill':
            if not self.release_line_ids:
                raise UserError(_('Choose the released employees to give.'))
            self.release_line_ids._ksw_decide('transfer', addition_line=self.addition_line_id,
                                              effective_date=self.effective_date, note=self.note)
        else:
            self.addition_line_id._ksw_decide_other(self.decision, self.quantity, self.note)
        return {'type': 'ir.actions.act_window_close'}


class KswEmployeeHireWizard(models.TransientModel):
    """HR names the employees hired from outside for a request."""
    _name = 'ksw.employee.hire.wizard'
    _description = 'Name New Hires'

    addition_line_id = fields.Many2one('ksw.employee.addition.line', required=True, readonly=True)
    department_id = fields.Many2one(related='addition_line_id.department_id')
    hire_open = fields.Integer(related='addition_line_id.hire_open')
    # The public model: transfer HR may hold no Employees rights; ids match.
    employee_ids = fields.Many2many('hr.employee.public', 'ksw_emp_hire_wizard_public_rel', 'wizard_id',
                                    'employee_id', string='New Employees', required=True,
                                    domain="[('department_id', 'child_of', department_id)]")

    def action_confirm(self):
        self.ensure_one()
        self.addition_line_id._ksw_record_hires(self.env['hr.employee'].browse(self.employee_ids.ids))
        return {'type': 'ir.actions.act_window_close'}
