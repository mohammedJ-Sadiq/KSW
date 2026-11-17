import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')


def number_key(text):
    return re.sub(r'\D', '', (text or '').translate(_DIGITS))


class KswCompanyRegistration(models.Model):
    """A commercial registration the company works under: its main CR, its
    branches (sub-CRs: transport, workshop, Jubail...) and the group's other
    companies whose papers the same HR keeps. A company document belongs to
    the registration whose number it carries; that number is how a scan is
    tied to its company."""
    _name = 'ksw.company.registration'
    _description = 'Commercial Registration'
    _order = 'company_id, kind, name'

    name = fields.Char(string='Trade Name', required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company,
                                 ondelete='cascade', index=True)
    kind = fields.Selection([
        ('main', 'Main CR'),
        ('branch', 'Branch (Sub-CR)'),
        ('group', 'Group Company (separate legal entity)'),
    ], required=True, default='branch')
    unified_number = fields.Char(string='Unified No. (700)', required=True, index=True,
                                 help='الرقم الوطني الموحد')
    cr_number = fields.Char(string='CR No.', index=True, help='رقم السجل التجاري')
    parent_id = fields.Many2one('ksw.company.registration', string='Main CR', index=True,
                                domain="[('company_id', '=', company_id), ('kind', '=', 'main')]")
    city = fields.Char()
    issue_date = fields.Date()
    active = fields.Boolean(default=True)
    document_ids = fields.One2many('ksw.document', 'registration_id', string='Documents')

    _unified_unique = models.Constraint('unique(unified_number)', 'This unified number is already registered.')

    @api.constrains('unified_number', 'cr_number')
    def _check_numbers(self):
        for reg in self:
            if not re.fullmatch(r'7\d{9}', number_key(reg.unified_number)):
                raise ValidationError(_('A unified number has 10 digits and starts with 7 (%s).', reg.name))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            for f in ('unified_number', 'cr_number'):
                if vals.get(f):
                    vals[f] = number_key(vals[f])
        return super().create(vals_list)

    def write(self, vals):
        for f in ('unified_number', 'cr_number'):
            if vals.get(f):
                vals[f] = number_key(vals[f])
        return super().write(vals)

    @api.model
    def _find(self, *numbers):
        """The registration carrying any of these numbers (unified or CR)."""
        keys = [k for k in map(number_key, numbers) if k]
        if not keys:
            return self.browse()
        found = self.sudo().with_context(active_test=False).search(
            ['|', ('unified_number', 'in', keys), ('cr_number', 'in', keys)])
        return found if len(found) == 1 else self.browse()


class ResCompany(models.Model):
    _inherit = 'res.company'

    x_registration_ids = fields.One2many('ksw.company.registration', 'company_id',
                                         string='Commercial Registrations')


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # Which registration (main CR or branch) the employee is under at Muqeem /
    # GOSI. HR-only like every KSW employee field (gotcha #32).
    x_registration_id = fields.Many2one('ksw.company.registration', string='Registered Under (CR)',
                                        groups='hr.group_hr_user', tracking=True)
