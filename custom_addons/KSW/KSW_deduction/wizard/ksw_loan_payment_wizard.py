from datetime import date

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class KswLoanPaymentWizard(models.TransientModel):
    """Record a payment made outside payroll against a deduction.

    Works for every type — loans, salary advances and penalties alike.
    Who may open it is decided per-record by
    ``ksw.deduction.x_can_edit_installments``: accounting
    (``group_installment_edit``) can close any type, HR
    (``group_hr_deduction_officer`` / ``group_loan_hr``) can close
    HR-managed types only. The model keeps its historical
    ``ksw.loan.payment.wizard`` name.

    Full payment (payment_amount == outstanding) always behaves the same:
    every pending installment is stamped paid/manual in-place, no extra row.

    Partial payments follow the chosen ``application_mode``:

    ``sequential`` (default) — settle the earliest installments first:
        Pending installments are consumed in due order. Each one the payment
        fully covers is stamped paid in-place. The installment the payment
        lands *inside* is split: the covered part is stamped paid on the
        original line and the uncovered remainder becomes a new pending line
        in the SAME month. Later installments are untouched.

        e.g. 1,650 against 4 x 1,000 →
            #1 paid 1,000 | #2 paid 650 + new pending 350 (same month) |
            #3, #4 still 1,000 pending.

        This mirrors `ksw.deduction._settle_payslip_lines`, which splits the
        same way when a payslip can only afford part of an installment, so
        manual and payroll splits look identical in the Installments tab.

    ``redistribute`` — spread the remaining balance evenly:
        A single manual-paid line is created for the payment amount and all
        pending installment amounts are reduced proportionally so the grand
        total (paid + pending) still equals the loan amount.

    In both modes the parent's write() with O2M commands handles the
    total-consistency check atomically; sudo() is used because auth is
    pre-verified here.
    """

    _name = 'ksw.loan.payment.wizard'
    _description = 'Record Deduction Payment'

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
    application_mode = fields.Selection(
        [
            ('sequential', 'Settle earliest installments first'),
            ('redistribute', 'Redistribute across remaining installments'),
        ],
        string='Application Method',
        default='sequential', required=True,
        help="How the payment is applied to the schedule.\n\n"
             "• Settle earliest installments first: closes the nearest due "
             "installments one by one. The installment the payment lands "
             "inside is split — the covered part is marked paid and the "
             "remainder stays pending in the same month. Later installments "
             "keep their original amounts and dates.\n\n"
             "• Redistribute across remaining installments: records the "
             "payment as one manual line and spreads the remaining balance "
             "evenly over every pending installment.",
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
        help='Pending installments will each be reduced to this amount '
             '(redistribute mode only).',
    )
    # Sequential-mode preview
    seq_settled_count = fields.Integer(
        string='Installments Fully Settled',
        compute='_compute_summary',
    )
    seq_partial_label = fields.Char(
        string='Partially Settled Installment',
        compute='_compute_summary',
        help='Month of the installment the payment lands inside, if any.',
    )
    seq_partial_paid = fields.Monetary(
        string='Paid on That Installment',
        compute='_compute_summary',
    )
    seq_partial_remainder = fields.Monetary(
        string='Still Pending in That Month',
        compute='_compute_summary',
    )

    # ── Shared helpers ───────────────────────────────────────────────

    @staticmethod
    def _pending_in_due_order(ded):
        """Pending installments, earliest due month first.

        Sorted on the period rather than the raw sequence so that split
        remainders (which inherit their origin's sequence) and manually
        rescheduled lines still settle in true chronological order.
        """
        return ded.line_ids.filtered(
            lambda l: l.state == 'pending'
        ).sorted(lambda l: (l.period_date or date.max, l.sequence, l.id))

    def _plan_sequential(self, pending, payment):
        """Dry-run the sequential walk. No writes.

        Returns ``(settled, split_line, paid_part, remainder)`` where
        ``settled`` is the recordset of installments the payment closes
        entirely and ``split_line`` (possibly empty) is the one it lands
        inside, paying ``paid_part`` and leaving ``remainder`` pending.
        """
        cur = self.deduction_id.currency_id
        Line = self.env['ksw.deduction.line']
        settled = Line.browse()
        split_line = Line.browse()
        paid_part = remainder = 0.0
        left = cur.round(payment)
        for line in pending:
            if cur.is_zero(left) or left < 0:
                break
            if cur.compare_amounts(left, line.amount) >= 0:
                settled |= line
                left = cur.round(left - line.amount)
            else:
                split_line = line
                paid_part = left
                remainder = cur.round(line.amount - left)
                break
        return settled, split_line, paid_part, remainder

    @api.depends('deduction_id', 'payment_amount', 'application_mode')
    def _compute_summary(self):
        for wiz in self:
            ded = wiz.deduction_id
            pending = wiz._pending_in_due_order(ded)
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
            # Sequential preview
            settled, split_line, paid_part, remainder = wiz._plan_sequential(
                pending, payment)
            wiz.seq_settled_count = len(settled)
            wiz.seq_partial_label = split_line.display_name or ''
            wiz.seq_partial_paid = paid_part
            wiz.seq_partial_remainder = remainder

    # ── Confirm ──────────────────────────────────────────────────────

    def action_confirm(self):
        self.ensure_one()
        ded = self.deduction_id

        # Same per-record matrix the Installments tab uses: accounting
        # (Loan Modification = Full) closes any type, HR officers and HR
        # approvers close HR-managed types (advances, penalties). The
        # compute also requires the deduction to be active.
        if not self.env.su and not ded.x_can_edit_installments:
            raise UserError(_(
                "You are not allowed to record a payment on this "
                "deduction. Accounting staff with the 'Loan Modification: "
                "Full' privilege can settle any type; HR officers and HR "
                "approvers can settle HR-managed types (salary advances "
                "and penalties) only."
            ))

        pending = self._pending_in_due_order(ded)
        if not pending:
            raise UserError(_("This deduction has no pending installments."))

        cur = ded.currency_id
        outstanding = sum(pending.mapped('amount'))
        payment = self.payment_amount

        if payment <= 0:
            raise ValidationError(_("Payment amount must be greater than zero."))
        if cur.compare_amounts(payment, outstanding) > 0:
            raise ValidationError(_(
                "Payment amount (%(pay).2f) exceeds the outstanding "
                "balance (%(out).2f %(cur)s).",
                pay=payment,
                out=outstanding,
                cur=cur.name or '',
            ))

        today = self.payment_date
        note = (self.note or '').strip()
        user = self.env.user
        stamp = {
            'is_manual': True,
            'manual_by': user.id,
            'manual_date': today,
            'manual_note': note,
        }
        commands = []
        is_full = cur.compare_amounts(payment, outstanding) == 0
        sequential = self.application_mode == 'sequential'
        # Populated by the sequential branch for the chatter body.
        settled = split_line = self.env['ksw.deduction.line'].browse()
        paid_part = remainder = 0.0

        if is_full:
            # Mark every pending installment as paid in-place — no new row.
            for line in pending:
                commands.append((1, line.id, dict(stamp, state='paid')))
        elif sequential:
            # Settle the nearest due installments; split the one the
            # payment lands inside (same month, same sequence) exactly as
            # `_settle_payslip_lines` does for a payroll shortfall.
            settled, split_line, paid_part, remainder = self._plan_sequential(
                pending, payment)
            for line in settled:
                commands.append((1, line.id, dict(stamp, state='paid')))
            if split_line:
                commands.append((1, split_line.id, dict(
                    stamp, state='paid', amount=paid_part)))
                commands.append((0, 0, {
                    'sequence': split_line.sequence,
                    'year': split_line.year,
                    'month': split_line.month,
                    'amount': remainder,
                    'state': 'pending',
                    'split_origin_id': split_line.id,
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
            commands.append((0, 0, dict(
                stamp,
                amount=payment,
                year=today.year,
                month=today.month,
                state='paid',
                sequence=max_seq + 1,
            )))

        # Write atomically: ksw.deduction.write() sets
        # _skip_installment_total_check during O2M processing and validates
        # once at the end. sudo() bypasses the line-level privilege guards
        # (auth was already checked above).
        ded.sudo().write({'line_ids': commands})

        # Chatter
        cur_name = cur.name or ''
        note_part = (
            Markup('<br/><b>Note:</b> %(n)s') % {'n': note}
            if note else Markup('')
        )
        if is_full:
            body = Markup(
                '<strong>💵 Full Payment Recorded (Manual)</strong><br/>'
                '<b>By:</b> %(u)s<br/>'
                '<b>Amount:</b> %(pay).2f %(cur)s<br/>'
                '<b>Date:</b> %(d)s%(note)s'
            ) % {
                'u': user.name,
                'pay': payment,
                'cur': cur_name,
                'd': fields.Date.to_string(today),
                'note': note_part,
            }
        elif sequential:
            body = Markup(
                '<strong>💵 Payment Recorded (Manual) — earliest '
                'installments settled</strong><br/>'
                '<b>By:</b> %(u)s<br/>'
                '<b>Paid:</b> %(pay).2f %(cur)s<br/>'
                '<b>Date:</b> %(d)s%(note)s<br/>'
            ) % {
                'u': user.name,
                'pay': payment,
                'cur': cur_name,
                'd': fields.Date.to_string(today),
                'note': note_part,
            }
            if settled:
                body += Markup('<b>Fully settled:</b><br/>')
                for line in settled:
                    body += Markup(
                        '&nbsp;&nbsp;• %(label)s — %(amt).2f %(cur)s<br/>'
                    ) % {
                        'label': line.display_name,
                        'amt': line.amount,
                        'cur': cur_name,
                    }
            if split_line:
                body += Markup(
                    '<b>Partially settled:</b> %(label)s — %(paid).2f '
                    '%(cur)s paid, %(rem).2f %(cur)s still pending in the '
                    'same month<br/>'
                ) % {
                    'label': split_line.display_name,
                    'paid': paid_part,
                    'rem': remainder,
                    'cur': cur_name,
                }
            body += Markup('<b>Remaining balance:</b> %(rem).2f %(cur)s') % {
                'rem': cur.round(outstanding - payment),
                'cur': cur_name,
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
                'cur': cur_name,
                'rem': remaining,
                'n': len(pending),
                'per': self.new_installment_amount,
                'd': fields.Date.to_string(today),
                'note': note_part,
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
