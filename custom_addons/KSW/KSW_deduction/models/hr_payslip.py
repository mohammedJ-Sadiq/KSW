from odoo import api, fields, models

# Order in which pending installments are collected: penalties / advances
# before loans (lower payroll_priority first), then oldest period, then
# sequence. Used both for input injection and for shortfall allocation.
_KSW_LINE_ORDER = 'payroll_priority, period_date, sequence'


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    def compute_sheet(self):
        for payslip in self:
            self._inject_ksw_deduction_inputs(payslip)
        res = super().compute_sheet()
        # Second pass: if the salary can't cover every KSW deduction,
        # cap the input amounts by priority (so the net never goes
        # negative) and recompute. `super()` is used for the rerun so we
        # do not re-inject the full amounts.
        rerun = self.filtered(
            lambda s: s._ksw_apply_deduction_priority())
        if rerun:
            super(HrPayslip, rerun).compute_sheet()
        return res

    def _ksw_pending_lines_domain(self, employee_ids, date_to):
        """Pending installments collectable on a payslip ending `date_to`.

        No lower period bound: an installment left pending in an earlier
        month (because a past payslip could not afford it) is naturally
        picked up — i.e. the unaffordable remainder rolls forward.
        """
        return [
            ('employee_id', 'in', employee_ids),
            ('state', '=', 'pending'),
            ('period_date', '<=', date_to),
            ('deduction_id.state', '=', 'active'),
        ]

    @api.model
    def get_inputs(self, versions, date_from, date_to):
        """Extend base payslip input generation so pending KSW deduction
        installments appear in "Other Inputs" immediately upon payslip
        creation (i.e. at `onchange_employee` time), not only after
        Compute. `compute_sheet` still deletes + regenerates KSW_DED_*
        inputs, so this is just for the create/onchange path.
        """
        res = super().get_inputs(versions, date_from, date_to)
        if not versions or not date_from or not date_to:
            return res
        employees = versions.mapped('employee_id')
        if not employees:
            return res
        lines = self.env['ksw.deduction.line'].sudo().search(
            self._ksw_pending_lines_domain(employees.ids, date_to),
            order=_KSW_LINE_ORDER,
        )
        if not lines:
            return res
        # Map employee -> version (pick first matching version per employee)
        emp_version = {}
        for v in versions:
            emp_version.setdefault(v.employee_id.id, v.id)
        for line in lines:
            version_id = emp_version.get(line.employee_id.id)
            if not version_id:
                continue
            ded = line.deduction_id
            res.append({
                'name': '%s [%s] inst %d/%d' % (
                    ded.type_id.name, ded.name,
                    line.sequence, ded.installments),
                'code': 'KSW_DED_%d' % line.id,
                'amount': line.amount,
                'version_id': version_id,
            })
        return res

    def _inject_ksw_deduction_inputs(self, payslip):
        if not payslip.employee_id or not payslip.date_from or not payslip.date_to:
            return
        old = payslip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_'))
        if old:
            old.unlink()
        lines = self.env['ksw.deduction.line'].sudo().search(
            self._ksw_pending_lines_domain(
                [payslip.employee_id.id], payslip.date_to),
            order=_KSW_LINE_ORDER,
        )
        if not lines:
            return
        version_id = (
            payslip.version_id.id
            or (payslip.employee_id.current_version_id
                and payslip.employee_id.current_version_id.id)
        )
        if not version_id:
            return
        InputLine = self.env['hr.payslip.input'].sudo()
        seq = 50
        for line in lines:
            ded = line.deduction_id
            label = '%s [%s] inst %d/%d' % (
                ded.type_id.name, ded.name, line.sequence, ded.installments)
            InputLine.create({
                'payslip_id': payslip.id,
                'version_id': version_id,
                'name': label,
                'code': 'KSW_DED_%d' % line.id,
                'amount': line.amount,
                'sequence': seq,
            })
            seq += 1

    def _ksw_apply_deduction_priority(self):
        """Cap KSW deduction inputs so the net pay never goes negative.

        Collects the affordable amount in priority order (penalties before
        loans). Returns True if any input amount was reduced (caller must
        recompute the payslip), False if the salary covered everything.
        """
        self.ensure_one()
        inputs = self.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_')
            and i.code[8:].isdigit())
        if not inputs:
            return False
        cur = self.company_id.currency_id or self.env.company.currency_id
        ksw_total = sum(inputs.mapped('amount'))
        # Net was computed in pass 1 with the full KSW deductions applied;
        # adding them back gives the salary available before KSW.
        net = self.get_salary_line_total('NET')
        available = net + ksw_total
        if cur.compare_amounts(available, ksw_total) >= 0:
            return False  # everything affordable, no capping

        # Order inputs by the underlying line's priority (penalties first).
        prio = {}
        for inp in inputs:
            line = self.env['ksw.deduction.line'].browse(int(inp.code[8:]))
            prio[inp.id] = (
                line.payroll_priority, line.period_date or fields.Date.today(),
                line.sequence)
        ordered = inputs.sorted(key=lambda i: prio[i.id])

        remaining = max(available, 0.0)
        changed = False
        for inp in ordered:
            full = inp.amount
            alloc = min(full, remaining)
            alloc = cur.round(alloc)
            if cur.compare_amounts(alloc, full) != 0:
                inp.amount = alloc
                changed = True
            remaining = max(remaining - alloc, 0.0)
        return changed

    def write(self, vals):
        new_state = vals.get('state')
        prev = {s.id: s.state for s in self} if new_state else {}
        result = super().write(vals)
        if new_state:
            for slip in self:
                old = prev.get(slip.id)
                if new_state == 'done' and old != 'done':
                    self._sync_deductions_on_done(slip)
                elif new_state in ('draft', 'cancel') and old == 'done':
                    self._sync_deductions_on_reset(slip)
        return result

    def _sync_deductions_on_done(self, payslip):
        """Reconcile deduction lines with what the payslip actually
        collected. Each KSW_DED_* input amount is the *capped* amount
        (after `_ksw_apply_deduction_priority`):
          • full          → mark the line paid;
          • zero           → leave it pending (rolls forward next month);
          • partial        → split: pay the affordable part, forward the
                              remainder as a new pending line.
        """
        Line = self.env['ksw.deduction.line'].sudo()
        alloc = {}  # line_id -> collected amount
        for i in payslip.input_line_ids:
            if i.code and i.code.startswith('KSW_DED_') and i.code[8:].isdigit():
                alloc[int(i.code[8:])] = i.amount
        if not alloc:
            return
        self.env['ksw.deduction'].sudo()._settle_payslip_lines(
            Line.browse(list(alloc)), alloc, payslip)

    def _sync_deductions_on_reset(self, payslip):
        self.env['ksw.deduction'].sudo()._unmark_lines_paid(payslip)
