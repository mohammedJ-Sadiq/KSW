from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class KswWorkshopTechnician(models.Model):
    """Who can be named on a labor line: the workshop's own technicians, plus
    the one standing entry for work sent outside.

    A model of its own rather than a domain on `hr.employee`, because the
    picker has to offer one thing that is not a person — "External Service
    Location". The alternative, a placeholder employee record, would turn up
    in every employee picker in the database and inflate the headcount: the
    exact trap the legacy history import already walked into (83 placeholder
    employees, archived by the 19.0.7.0.0 migration).

    This is the single source of truth. The "Workshop Technician" checkbox on
    the employee form is a view onto it (see hr_employee.py), not a second
    store — ticking it creates the row here, unticking archives it — so there
    is no mirror to rot.
    """
    _name = 'ksw.workshop.technician'
    _description = 'Workshop Technician'
    # is_external sorts False first, so the people come first and "External
    # Service Location" sits at the bottom of the dropdown.
    _order = 'is_external, name'

    name = fields.Char(
        compute='_compute_name', store=True, readonly=False,
        help="Taken from the linked employee and kept in step if they are renamed. "
             "Free text for a technician who has no employee record.")
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', ondelete='cascade',
        help="Optional. Leave empty for someone who works in the shop but has no "
             "employee record in Odoo.")
    is_external = fields.Boolean(
        readonly=True,
        help="Marks the single built-in 'External Service Location' entry, which "
             "stands for any job sent outside the workshop.")
    active = fields.Boolean(default=True)
    note = fields.Char()

    # NULLs do not collide in a Postgres unique index, so several by-name-only
    # technicians are fine; only the employee link is one-to-one.
    _employee_uniq = models.Constraint(
        'unique(employee_id)',
        'This employee is already registered as a workshop technician.',
    )

    @api.constrains('name')
    def _check_name(self):
        for technician in self:
            if not (technician.name or '').strip():
                raise ValidationError(_(
                    'A workshop technician needs a name. Link an employee, or type one.'
                ))

    @api.depends('employee_id', 'employee_id.name')
    def _compute_name(self):
        for technician in self:
            if technician.employee_id:
                technician.name = technician.employee_id.name
            elif not technician.name:
                technician.name = False
