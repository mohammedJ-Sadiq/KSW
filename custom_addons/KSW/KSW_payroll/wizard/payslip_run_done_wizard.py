from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import UserError


class KswPayslipRunDoneWizard(models.TransientModel):
    """Mark As Done for some of a batch's paying accounts, not all.

    Each paying account is usually its own bank transfer, and they do not
    all go out on the same day. Confirming one account's payslips leaves
    the rest in draft, and the batch only turns Done with the last one.
    """
    _name = 'ksw.payslip.run.done.wizard'
    _description = 'Mark Payslip Batch Done by Bank Account'

    run_id = fields.Many2one('hr.payslip.run', required=True, readonly=True,
                             ondelete='cascade')
    line_ids = fields.One2many('ksw.payslip.run.done.wizard.line',
                               'wizard_id')

    def action_confirm(self):
        self.ensure_one()
        run = self.run_id
        chosen = self.line_ids.filtered('selected')
        if not chosen:
            raise UserError(_('Select at least one bank account to mark as done.'))
        # Resolved again now, not from the figures shown: a slip may have
        # been confirmed, cancelled or moved account since the dialog opened.
        groups = run._ksw_pending_bank_groups()
        wanted = set(chosen.mapped(lambda l: l.bank_account_id.id or False))
        slips = self.env['hr.payslip']
        for bank, bank_slips in groups.items():
            if (bank.id or False) in wanted:
                slips |= bank_slips
        if not slips:
            raise UserError(_('The selected bank accounts have no payslips left to confirm.'))
        run._ksw_confirm_slips(slips)
        if run.state != 'done':
            left = run._ksw_pending_bank_groups()
            body = Markup('<strong>%(title)s</strong><br/>') % {
                'title': _('Marked done for some bank accounts'),
            }
            for line in chosen:
                body += Markup('✅ %(bank)s: %(n)s<br/>') % {
                    'bank': line.label, 'n': line.slip_count,
                }
            for bank, bank_slips in left.items():
                body += Markup('⏳ %(bank)s: %(n)s<br/>') % {
                    'bank': bank.display_name or _('No bank account'),
                    'n': len(bank_slips),
                }
            run.message_post(body=body, subtype_xmlid='mail.mt_note')
        return {'type': 'ir.actions.act_window_close'}


class KswPayslipRunDoneWizardLine(models.TransientModel):
    _name = 'ksw.payslip.run.done.wizard.line'
    _description = 'Mark Payslip Batch Done: Bank Account'

    wizard_id = fields.Many2one('ksw.payslip.run.done.wizard', required=True,
                                ondelete='cascade')
    selected = fields.Boolean(string='Mark Done', default=True)
    bank_account_id = fields.Many2one('res.partner.bank', readonly=True)
    label = fields.Char(string='Bank Account', compute='_compute_label')
    slip_count = fields.Integer(string='Payslips', readonly=True)
    total_net = fields.Float(string='Total NET', readonly=True)

    def _compute_label(self):
        for line in self:
            line.label = (line.bank_account_id.display_name
                          or _('No bank account'))
