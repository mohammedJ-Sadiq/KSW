from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class KswLoanPaymentWizard(models.TransientModel):
    """Record a payment made outside payroll against a loan.

    Two modes (auto-detected from the payment amount):

    Full payment  — payment_amount == total outstanding:
        Every pending installment is stamped paid/manual in-place.
        No extra row is created.

    Partial payment — payment_amount < total outstanding:
        A single manual-paid line is created for the payment amount.
        All pending installment amounts are reduced proportionally so
        the grand total (paid + pending) still equals the loan amount.

    The parent's write() with O2M commands handles the constraint
    check atomically; sudo() is used because auth is pre-verified here.
    """

    _name = 'ksw.loan.payment.wizard'
    _description = 'Record Loan Payment'

    deduction_id = fields.Many2one(
        'ksw.deduction', required=True, readonly=True, ondelete='cascade',
    )
    currency_id = fields.Many2one(
        related='deduction_id.currency_id', readonly=True,
    )
    employee_id = fields.Many2one(
        related='deduction_id.employee_id', readonly=True,
    )

    # ── Input ────────────────────────────────────────────────────────
    payment_amount = fields.Monetary(
        string='Payment Amount', required=True,
        help='Amount the employee paid outside the payroll cycle.',
    )
    payment_date = fields.Date(
        required=True,
        default=lambda s: fields.Date.context_today(s),
    )
    note = fields.Char(
        string='Reference / Note',
        help='Receipt number, bank reference, or any other identifier.',
    )

    # ── Read-only summary (non-stored computes) ───────────────────────
    total_outstanding = fields.Monetary(
        string='Outstanding Before Payment',
        compute='_compute_summary',
    )
    pending_count = fields.Integer(
        string='Pending Installments',
        compute='_compute_summary',
    )
    is_full_payment = fields.Boolean(
        compute='_compute_summary',
        help='True when payment_amount covers the full outstanding balance.',
    )
    remaining_after = fields.Monetary(
        string='Remaining After Payment',
        compute='_compute_summary',
    )
    new_installment_amount = fields.Monetary(
        string='New Amount / Installment',
        compute='_compute_summary',
        help='Pending installments will each be reduced to this amount.',
    )

    @api.depends('deduction_id', 'payment_amount')
    def _compute_summary(self):
        for wiz in self:
            ded = wiz.deduction_id
            pending = ded.line_ids.filtered(lambda l: l.state == 'pending')
            outstanding = sum(pending.mapped('amount'))
            wiz.total_outstanding = outstanding
            wiz.pending_count = len(pending)
            payment = wiz.payment_amount or 0.0
            remaining = max(outstanding - payment, 0.0)
            wiz.remaining_after = remaining
            wiz.is_full_payment = (
                ded.currency_id.compare_amounts(payment, outstanding) >= 0
            )
            n = len(pending)
            wiz.new_installment_amount = (
                round(remaining / n, 2) if n and remaining > 0 else 0.0
            )

    # ── Confirm ──────────────────────────────────────────────────────

    def action_confirm(self):
        self.ensure_one()
        ded = self.deduction_id

        if not self.env.su:
            if not self.env.user.has_group(
                    'KSW_deduction.group_installment_edit'):
                raise UserError(_(
                    "Recording a loan payment requires the 'Loan "
                    "Installment Modification' privilege."
                ))

        pending = ded.line_ids.filtered(
            lambda l: l.state == 'pending').sorted('sequence')
        if not pending:
            raise UserError(_("This loan has no pending installments."))

        outstanding = sum(pending.mapped('amount'))
        payment = self.payment_amount

        if payment <= 0:
            raise ValidationError(_("Payment amount must be greater than zero."))
        if ded.currency_id.compare_amounts(payment, outstanding) > 0:
            raise ValidationError(_(
                "Payment amount (%(pay).2f) exceeds the outstanding "
                "balance (%(out).2f %(cur)s).",
                pay=payment,
                out=outstanding,
                cur=ded.currency_id.name or '',
            ))

        today = self.payment_date
        note = (self.note or '').strip()
        user = self.env.user
        commands = []
        is_full = ded.currency_id.compare_amounts(payment, outstanding) == 0

        if is_full:
            # Mark every pending installment as paid in-place — no new row.
            for line in pending:
                commands.append((1, line.id, {
                    'state': 'paid',
                    'is_manual': True,
                    'manual_by': user.id,
                    'manual_date': today,
                    'manual_note': note,
                }))
        else:
            # Redistribute the remaining balance equally across all pending
            # installments, then add one manual paid line for the payment.
            remaining = outstanding - payment
            n = len(pending)
            per = round(remaining / n, 2)
            running = 0.0
            for i, line in enumerate(pending):
                if i < n - 1:
                    new_amt = per
                    running += per
                else:
                    new_amt = round(remaining - running, 2)  # absorb residue
                commands.append((1, line.id, {'amount': new_amt}))
            max_seq = max(pending.mapped('sequence') or [0])
            commands.append((0, 0, {
                'amount': payment,
                'year': today.year,
                'month': today.month,
                'state': 'paid',
                'is_manual': True,
                'manual_by': user.id,
                'manual_date': today,
                'manual_note': note,
                'sequence': max_seq + 1,
            }))

        # Write atomically: ksw.deduction.write() sets
        # _skip_installment_total_check during O2M processing and validates
        # once at the end. sudo() bypasses the line-level privilege guards
        # (auth was already checked above).
        ded.sudo().write({'line_ids': commands})

        # Chatter
        cur = ded.currency_id.name or ''
        if is_full:
            body = Markup(
                '<strong>💵 Full Payment Recorded (Manual)</strong><br/>'
                '<b>By:</b> %(u)s<br/>'
                '<b>Amount:</b> %(pay).2f %(cur)s<br/>'
                '<b>Date:</b> %(d)s%(note)s'
            ) % {
                'u': user.name,
                'pay': payment,
                'cur': cur,
                'd': fields.Date.to_string(today),
                'note': ('<br/><b>Note:</b> %s' % note) if note else '',
            }
        else:
            body = Markup(
                '<strong>💵 Partial Payment Recorded (Manual)</strong><br/>'
                '<b>By:</b> %(u)s<br/>'
                '<b>Paid:</b> %(pay).2f %(cur)s<br/>'
                '<b>Remaining balance:</b> %(rem).2f %(cur)s '
                '(%(n)d installments × %(per).2f %(cur)s)<br/>'
                '<b>Date:</b> %(d)s%(note)s'
            ) % {
                'u': user.name,
                'pay': payment,
                'cur': cur,
                'rem': remaining,
                'n': len(pending),
                'per': self.new_installment_amount,
                'd': fields.Date.to_string(today),
                'note': ('<br/><b>Note:</b> %s' % note) if note else '',
            }
        ded.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')

        # Auto-complete if all lines are now paid
        if all(l.state == 'paid' for l in ded.sudo().line_ids):
            ded.sudo().write({'state': 'completed'})
            ded.sudo().message_post(
                body=Markup(
                    '<strong>🏁 Completed</strong> — all installments paid.'
                ),
                subtype_xmlid='mail.mt_note',
            )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.deduction',
            'res_id': ded.id,
            'view_mode': 'form',
            'target': 'current',
        }
