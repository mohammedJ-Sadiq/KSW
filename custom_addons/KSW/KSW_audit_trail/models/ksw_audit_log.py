from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswAuditLog(models.Model):
    """One changed value on a user, employee, contract or salary bank account.

    Written only by the system (sudo) from the audited models' create/write/
    unlink. The ACL grants read and nothing else, and write/unlink refuse
    even an administrator in the UI: an audit trail somebody can tidy is not
    an audit trail. Only code running as superuser (a migration, a shell)
    can remove rows.
    """
    _name = 'ksw.audit.log'
    _description = 'Audit Log'
    _order = 'id desc'
    _rec_name = 'record_name'

    date = fields.Datetime(default=fields.Datetime.now, readonly=True, index=True)
    user_id = fields.Many2one('res.users', string='Changed By', readonly=True,
                              index=True, ondelete='set null')
    operation = fields.Selection([
        ('create', 'Created'),
        ('write', 'Changed'),
        ('unlink', 'Deleted'),
    ], readonly=True, required=True)
    model = fields.Char(readonly=True, index=True)
    model_label = fields.Char(string='Record Type', readonly=True)
    res_id = fields.Integer(string='Record ID', readonly=True, index=True)
    record_name = fields.Char(string='Record', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Employee',
                                  readonly=True, index=True,
                                  ondelete='set null')
    field_name = fields.Char(string='Field (technical)', readonly=True)
    field_label = fields.Char(string='Field', readonly=True)
    old_value = fields.Text(string='Old Value', readonly=True)
    new_value = fields.Text(string='New Value', readonly=True)

    def write(self, vals):
        if not self.env.su:
            raise UserError(_('Audit log entries cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_('Audit log entries cannot be deleted.'))
        return super().unlink()

    @api.model
    def _log(self, entries):
        if entries:
            self.sudo().create(entries)
