from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import UserError


class KswPayRunPaidWizard(models.TransientModel):
    """Mark Paid for some of a month's paying accounts, not all.

    Each paying account is its own transfer. The lines of the ticked
    accounts are marked paid; the run turns Paid with the last account.
    """
    _name = 'ksw.pay.run.paid.wizard'
    _description = 'Mark Pay Run Paid by Bank Account'

    run_id = fields.Many2one('ksw.pay.run', required=True, readonly=True,
                             ondelete='cascade')
    line_ids = fields.One2many('ksw.pay.run.paid.wizard.line', 'wizard_id')

    def action_confirm(self):
        self.ensure_one()
        run = self.run_id
        chosen = self.line_ids.filtered('selected')
        if not chosen:
            raise UserError(_('Select at least one bank account to mark as paid.'))
        # Resolved again now, not from the figures shown.
        wanted = set(chosen.mapped(lambda l: l.bank_account_id.id or False))
        lines = self.env['ksw.pay.run.line']
        for bank, bank_lines in run._unpaid_bank_groups().items():
            if (bank.id or False) in wanted:
                lines |= bank_lines
        if not lines:
            raise UserError(_('The selected bank accounts have nothing left to mark as paid.'))
        run._mark_lines_paid(lines)
        body = Markup('<strong>%(title)s</strong><br/>') % {
            'title': _('Marked paid for some bank accounts')
            if run.state != 'paid' else _('Marked paid'),
        }
        for line in chosen:
            body += Markup('✅ %(bank)s: %(n)s<br/>') % {
                'bank': line.label, 'n': line.line_count,
            }
        for bank, bank_lines in run._unpaid_bank_groups().items():
            body += Markup('⏳ %(bank)s: %(n)s<br/>') % {
                'bank': bank.display_name or _('No bank account'),
                'n': len(bank_lines),
            }
        run.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
        return {'type': 'ir.actions.act_window_close'}


class KswPayRunPaidWizardLine(models.TransientModel):
    _name = 'ksw.pay.run.paid.wizard.line'
    _description = 'Mark Pay Run Paid: Bank Account'

    wizard_id = fields.Many2one('ksw.pay.run.paid.wizard', required=True,
                                ondelete='cascade')
    selected = fields.Boolean(string='Mark Paid', default=True)
    bank_account_id = fields.Many2one('res.partner.bank', readonly=True)
    label = fields.Char(string='Bank Account', compute='_compute_label')
    line_count = fields.Integer(string='Employees', readonly=True)
    total_payable = fields.Float(string='Total Payable', readonly=True)

    def _compute_label(self):
        for line in self:
            line.label = (line.bank_account_id.display_name
                          or _('No bank account'))
