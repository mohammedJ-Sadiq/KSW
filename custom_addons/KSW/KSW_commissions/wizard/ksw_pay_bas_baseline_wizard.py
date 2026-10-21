"""Load a voucher already in BAS as a month's starting point.

Months exported before the voucher ledger existed are already in BAS, and
the ledger knows nothing of them: the first journal export would post the
month a second time. This reads the BAS voucher (read-only, through
KSW_ext_sync's connector), matches each line to the ledger's own keys from
the description our export wrote on it (``<pay type> <employee> شهر
<month>``), and records it as posted without a file. From then on the
month's exports post only the difference.

A line that matches nothing in the month today is kept under its own key
(``bas:<voucher>:<serial>``), so the next export reverses it rather than
leaving BAS with a figure Odoo no longer has. Lines on accounts the month
does not use (the sales commission sharing the voucher) are left out and
listed.
"""
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class KswPayBasBaselineWizard(models.TransientModel):
    _name = 'ksw.pay.bas.baseline.wizard'
    _description = 'Load a BAS Voucher as the Month Starting Point'

    run_id = fields.Many2one('ksw.pay.run', required=True, readonly=True)
    voucher_number = fields.Char(
        string='BAS Voucher Number', required=True,
        help='The number of the voucher already in BAS for this month, '
             'e.g. 26008282.')
    preview = fields.Text(readonly=True)
    previewed = fields.Boolean(readonly=True)

    # ------------------------------------------------------------------
    def _read_bas(self):
        number = (self.voucher_number or '').strip()
        if not number.isdigit():
            raise UserError(_('Enter the BAS voucher number (digits only).'))
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(
                "SELECT SERIAL, FDATE, FCODE, TCODE, AMOUNT, REF, NAME "
                "FROM VOU10 WHERE NUMBER1 = %s AND CODE2 = '010' "
                "ORDER BY SERIAL", (int(number),))
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            raise UserError(_('BAS has no voucher %(number)s.', number=number))
        return number, rows

    def _match(self):
        """``(items, report)`` — the ledger items the BAS voucher amounts
        to, and what could not be placed."""
        self.ensure_one()
        # The ledger's keys are in BAS's language whoever loads the voucher.
        lang = self.env['ksw.pay.settlement']._voucher_lang()
        if lang and self.env.context.get('lang') != lang:
            return self.with_context(lang=lang)._match()
        run = self.run_id.sudo()
        Settlement = self.env['ksw.pay.settlement'].sudo()
        Journal = self.env['ksw.bas.journal']
        lang = Journal.voucher_lang()
        number, bas_rows = self._read_bas()
        desired = Settlement._desired_events(run)

        # "شهر أغسطس" exactly as the export wrote it, from the same writer.
        voiced = run.with_context(lang=lang)
        suffix = ' '.join(voiced._bas_ref(
            '', '', Journal.with_context(lang=lang).month_label(run.period)
        ).split())
        people = (run._all_entries().employee_id
                  | run.line_ids.employee_id).sudo()
        Employee = self.env['hr.employee'].sudo().with_context(
            active_test=False)

        def norm(text):
            # Hand-typed lines spell the same words differently (اغسطس /
            # أغسطس); compare on one spelling, whitespace collapsed.
            text = ' '.join((text or '').split())
            for a in 'أإآ':
                text = text.replace(a, 'ا')
            return text.casefold()

        # The month's own people first, then everyone: a line for a man no
        # longer in the month must still be recognised as his.
        names = {}
        for employee in people | Employee.search([]):
            for label in (Journal.employee_label(employee),
                          (employee.name or '').strip()):
                if label:
                    names.setdefault(norm(label), employee)
        month_word = norm(Journal.with_context(lang=lang).month_label(run.period))
        components = self.env['ksw.pay.component'].sudo().with_context(
            active_test=False).search([])
        expense_codes = set(components.mapped('x_bas_expense_code')) - {False}
        accrual_codes = set(components.mapped('x_bas_accrual_code')) - {False}

        def who_of(ref):
            """``(employee, pay-type label)`` from a line's description."""
            text = norm(ref)
            for tail in (norm(suffix), month_word):
                if tail and text.endswith(tail):
                    text = text[:-len(tail)].strip()
                    break
            best = None
            for label, employee in names.items():
                if text.endswith(label) and (
                        best is None or len(label) > len(best[0])):
                    best = (label, employee)
            raw = ' '.join((ref or '').split())
            if not best:
                return None, raw
            # norm() swaps characters one for one on the collapsed text, so
            # positions in it are positions in the raw description.
            return best[1], raw[:len(text) - len(best[0])].strip()

        def accrual_for(expense, label):
            same = components.filtered(
                lambda c: c.x_bas_expense_code == expense)
            pick = same.filtered(
                lambda c: norm(c.x_bas_ref or c.name) == norm(label))[:1] \
                or same[:1]
            return pick.x_bas_accrual_code, pick.x_bas_accrual_name

        # BAS's own structure: each man's lines end with his accrual credit
        # (21070…). A block whose debits are on this month's expense accounts
        # is this month's, every line of it, whatever its description says.
        blocks, current = [], []
        for row in bas_rows:
            current.append(row)
            if (row['TCODE'] or '').startswith('2107'):
                blocks.append(current)
                current = []
        if current:
            blocks.append(current)
        # A gross voucher writes each deduction after the man's accrual
        # credit (Dr accrual / Cr advance): a block with no expense line in
        # it continues the one before.
        merged = []
        for block in blocks:
            has_expense = any(
                r['FCODE'] and not r['FCODE'].startswith(('2107', '1205'))
                for r in block)
            if merged and not has_expense:
                merged[-1] = merged[-1] + block
            else:
                merged.append(block)
        blocks = merged

        items = {}
        used = set()
        report = {'skipped': defaultdict(float), 'orphans': [],
                  'credits': defaultdict(float), 'expense': defaultdict(float),
                  'loans': defaultdict(float), 'accrual_of': {}}
        for block in blocks:
            if not any((r['FCODE'] or '') in expense_codes for r in block):
                for row in block:
                    report['skipped'][row['FCODE'] or row['TCODE'] or '?'] += \
                        round(row['AMOUNT'] or 0.0, 2)
                continue
            # Whose block: the first line naming him, else his advance account.
            employee = Employee
            for row in block:
                found, _label = who_of(row['REF'])
                if found:
                    employee = found
                    break
            if not employee:
                for row in block:
                    tcode = row['TCODE'] or ''
                    if tcode.startswith('1205'):
                        employee = Employee.search(
                            [('x_loan_acc_no', '=', tcode)], limit=1)
                        if employee:
                            break
            owner = employee
            for row in block:
                amount = round(row['AMOUNT'] or 0.0, 2)
                fcode, tcode = row['FCODE'] or '', row['TCODE'] or ''
                found, label = who_of(row['REF'])
                # A line naming someone is his, even inside another man's
                # block (a block netting to zero has no accrual line to close
                # it, so the next man's lines run on into it). Only unnamed
                # lines fall back on the block's owner.
                employee = found or owner
                if not found:
                    label = ' '.join((row['REF'] or '').split())
                if fcode in expense_codes:
                    code, name = accrual_for(fcode, label)
                    vals = None
                    if employee:
                        report['expense'][employee.id] += amount
                        candidates = [
                            e for e in desired.values()
                            if e['kind'] == 'accrual'
                            and e['employee_id'] == employee.id
                            and e['expense_code'] == fcode]
                        # Same wording; else the only pay type on that
                        # account; else the one whose figure matches (the
                        # wording changed since — «العمل الإضافي» is today's
                        # «بدل عمل اضافي»).
                        same_amount = [e for e in candidates
                                       if abs(e['amount'] - amount) < 0.005]
                        pick = ([e for e in candidates
                                 if norm(e['label']) == norm(label)]
                                or (candidates if len(candidates) == 1 else [])
                                or (same_amount if len(same_amount) == 1
                                    else []))
                        if pick:
                            vals = dict(pick[0], amount=0.0)
                        else:
                            vals = {
                                'key': 'accrual:%d:%d:%s|%s|%s' % (
                                    run.id, employee.id, fcode, code or '',
                                    label),
                                'kind': 'accrual', 'run_id': run.id,
                                'employee_id': employee.id, 'label': label,
                                'expense_code': fcode,
                                'expense_name': row['NAME'],
                                'accrual_code': code, 'accrual_name': name,
                                'amount': 0.0,
                            }
                    if vals is None:
                        vals = {
                            'key': 'bas:%s:%s' % (number, row['SERIAL']),
                            'kind': 'accrual', 'run_id': run.id,
                            'label': ' '.join((row['REF'] or '').split()),
                            'expense_code': fcode, 'expense_name': row['NAME'],
                            'accrual_code': code, 'accrual_name': name,
                            'amount': 0.0,
                        }
                    if vals['key'] not in desired:
                        report['orphans'].append(
                            ' '.join((row['REF'] or '').split()))
                    item = items.setdefault(vals['key'], vals)
                    item['amount'] = round(item['amount'] + amount, 2)
                elif tcode.startswith('2107') or fcode.startswith('2107'):
                    # His accrual: credited net of his deductions (or, in a
                    # gross voucher, debited for each). Placed by the other
                    # lines; kept only to check the block nets to zero.
                    if employee:
                        report['credits'][employee.id] += (
                            amount if tcode else -amount)
                        if tcode:
                            report['accrual_of'][employee.id] = (
                                tcode, row['NAME'])
                elif tcode.startswith('1205'):
                    if employee:
                        report['loans'][employee.id] += amount
                    candidates = [
                        e for e in desired.values()
                        if e['kind'] == 'deduction' and employee
                        and e['employee_id'] == employee.id
                        and abs(e['amount'] - amount) < 0.005
                        and e['key'] not in used]
                    if candidates:
                        vals = dict(candidates[0])
                        used.add(vals['key'])
                    else:
                        vals = {
                            'key': 'bas:%s:%s' % (number, row['SERIAL']),
                            'kind': 'deduction', 'run_id': run.id,
                            'employee_id': employee.id,
                            'label': ' '.join((row['REF'] or '').split()),
                            'amount': amount,
                        }
                        report['orphans'].append(vals['label'] or tcode)
                    vals.update(counter_code=tcode, counter_name=row['NAME'])
                    items[vals['key']] = vals
                else:
                    report['skipped'][fcode or tcode] += amount

        # The accrual each deduction drew on is the one BAS credited him.
        default_accrual = next(iter(sorted(accrual_codes)), '')
        for vals in items.values():
            if vals['kind'] == 'deduction':
                code, name = report['accrual_of'].get(
                    vals.get('employee_id'), (default_accrual, ''))
                vals['accrual_split'] = [[code, name, vals['amount']]]
            vals['event_date'] = bas_rows[0]['FDATE'].date()
        report['number'] = number
        report['date'] = bas_rows[0]['FDATE'].date()
        report['desired'] = desired
        return list(items.values()), report

    def _summary(self, items, report):
        desired = report['desired']
        loaded = {i['key']: i['amount'] for i in items}
        right_bas = sum(i['amount'] for i in items if i['kind'] == 'accrual')
        ded_bas = sum(i['amount'] for i in items if i['kind'] == 'deduction')
        right_now = sum(e['amount'] for e in desired.values()
                        if e['kind'] == 'accrual')
        ded_now = sum(e['amount'] for e in desired.values()
                      if e['kind'] == 'deduction')
        diff = defaultdict(float)
        register = set(self.run_id.sudo().line_ids.employee_id.ids)
        vacation = self.env['ksw.pay.settlement'].sudo()._vacation_parts(
            self.run_id.sudo())
        settled_elsewhere = []
        for key in set(loaded) | set(desired):
            item = next((i for i in items if i['key'] == key), None)
            if key not in desired and (item or {}).get('employee_id') \
                    not in register:
                settled_elsewhere.append(item)
                continue
            target = desired.get(key, {}).get('amount', 0.0)
            if vacation.get(key):
                target += min(max(loaded.get(key, 0.0) - target, 0.0),
                              vacation[key])
            d = round(target - loaded.get(key, 0.0), 2)
            if d:
                diff[(desired.get(key) or item)['kind']] += d
        report['settled_elsewhere'] = settled_elsewhere
        report['reversed'] = [
            i for i in items
            if i['key'] not in desired and i.get('employee_id') in register]
        unbalanced = [
            emp_id for emp_id, expense in report['expense'].items()
            if abs(expense - report['loans'].get(emp_id, 0.0)
                   - report['credits'].get(emp_id, 0.0)) >= 0.005]
        lines = [
            _('BAS voucher %(number)s of %(date)s', number=report['number'],
              date=report['date']),
            '',
            _('In BAS for this month: right %(right).2f, deductions %(ded).2f',
              right=right_bas, ded=ded_bas),
            _('The month in Odoo now: right %(right).2f, deductions %(ded).2f',
              right=right_now, ded=ded_now),
            '',
            _('The next journal export will post the difference:'),
            _('  right %(r)+.2f · deductions %(d)+.2f',
              r=diff.get('accrual', 0.0), d=diff.get('deduction', 0.0)),
        ]
        elsewhere = report.get('settled_elsewhere') or []
        if elsewhere:
            lines += ['', _('Left as they are in BAS: %(n)s line(s), %(amount).2f, '
                            'for people no longer in the month\'s register '
                            '(paid on a vacation payslip, held, or settled '
                            'by an entry of their own):',
                            n=len(elsewhere),
                            amount=sum(i['amount'] for i in elsewhere))]
            lines += ['  • %s' % self._describe(i) for i in elsewhere[:20]]
        if report.get('reversed'):
            lines += ['', _('Reversed by the next export: %(n)s line(s) for '
                            'people still in the register whose figures '
                            'changed:', n=len(report['reversed']))]
            lines += ['  • %s' % self._describe(i)
                      for i in report['reversed'][:20]]
        if report['skipped']:
            lines += ['', _('Left out (accounts this month does not use, e.g. '
                            'sales commission):')]
            lines += ['  • %s: %.2f' % (code, amount)
                      for code, amount in sorted(report['skipped'].items())]
        if unbalanced:
            lines += ['', _('%(n)s employee(s) whose BAS lines do not net to '
                            'zero (expense − deductions − accrual):',
                            n=len(unbalanced))]
        return '\n'.join(lines)

    def _describe(self, item):
        employee = self.env['hr.employee'].sudo().browse(
            item.get('employee_id') or [])
        who = (employee.name or '').strip()
        label = item.get('label') or ''
        return '%s%s %.2f' % ('%s — ' % who if who and who not in label else '',
                              label, item['amount'])

    # ------------------------------------------------------------------
    def _check(self):
        self.ensure_one()
        self.env['ksw.pay.settlement']._check_accountant()
        run = self.run_id
        if run.state not in ('approved', 'paid'):
            raise UserError(_('Only an approved or paid month can be loaded.'))
        if run._bas_vouchers().filtered(lambda v: v.state != 'cancel'):
            raise UserError(_(
                'This month already has BAS vouchers. A voucher loaded from BAS '
                'is the starting point, so it can only be the first.'))
        # A cancelled load holds nothing: the same voucher may be loaded again.
        if self.env['ksw.pay.settlement'].sudo().search_count([
                ('bas_voucher_number', '=', (self.voucher_number or '').strip()),
                ('state', '!=', 'cancel')]):
            raise UserError(_('BAS voucher %(n)s is already loaded.',
                              n=self.voucher_number))

    def action_preview(self):
        self._check()
        items, report = self._match()
        self.write({'preview': self._summary(items, report),
                    'previewed': True})
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name,
            'res_id': self.id, 'view_mode': 'form', 'target': 'new',
            'name': _('Load BAS Voucher'),
        }

    def action_load(self):
        self._check()
        items, report = self._match()
        Settlement = self.env['ksw.pay.settlement']
        voucher = Settlement.create({
            'origin': 'baseline',
            'run_id': self.run_id.id,
            'date': report['date'],
            'journal_number': report['number'],
            'bas_voucher_number': report['number'],
            'note': self._summary(items, report),
        })
        self.env['ksw.pay.settlement.item'].sudo().create([
            dict(vals, settlement_id=voucher.id) for vals in items])
        voucher._mark_done(manual=True)
        return voucher._open_form()
