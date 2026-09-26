"""Bank-file export wizard for the monthly commission payment.

Generates WPS Excel / Kawthar TXT files from ``net_payable`` on each
``ksw.pay.run.line`` — the payment register built when the General Manager
approves the month. Same file formats as ``KSW_payroll``; different source.
"""
import base64
import io
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    openpyxl = None


EXPORT_MODES = [
    ('all_excel',    'All banks – Excel files'),
    ('all_txt',      'All banks – Text files (Kawthar)'),
    ('specific_excel', 'Specific bank – Excel'),
    ('specific_txt', 'Specific bank – Text file'),
    ('journal_entry', 'Journal entries – BAS import file'),
]


class KswCommissionBankExportWizard(models.TransientModel):
    _name = 'ksw.commission.bank.export.wizard'
    _description = 'KSW Commission Bank File Export Wizard'

    run_id = fields.Many2one(
        'ksw.pay.run', required=True, readonly=True,
    )
    export_mode = fields.Selection(
        EXPORT_MODES, required=True, default='all_excel',
    )
    bank_account_id = fields.Many2one(
        'res.partner.bank',
        domain="[('x_file_type', '!=', False),"
               " ('partner_id', '=', company_partner_id)]",
    )
    company_partner_id = fields.Many2one(
        'res.partner', compute='_compute_company_partner',
    )
    value_date = fields.Date(
        default=fields.Date.context_today,
        help='Payment value date used in TXT files.',
    )
    operation_code = fields.Selection(
        [('1', '1 – New'), ('2', '2 – Renewal'), ('3', '3 – Delete')],
        default='2', string='Kawthar Operation',
    )
    journal_number = fields.Char(
        string='Journal Number',
        help='The voucher number in BAS. Written in the "number" column of '
             'every line of the journal-entry file.',
    )
    excel_layout = fields.Selection(
        [('merged', 'One file: every bank in one workbook'),
         ('split', 'One file per bank (zip)')],
        default='merged', required=True, string='Excel Files',
        help='Merged: one workbook with the summary over every employee and '
             'one sheet per paying bank.',
    )

    @api.depends('run_id')
    def _compute_company_partner(self):
        for rec in self:
            rec.company_partner_id = rec.env.company.partner_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _group_and_validate(self, require_type=None):
        run = self.run_id
        if not run.line_ids:
            raise UserError(_('The payment register is empty — there is nothing to export.'))
        groups = run._group_lines_by_bank_account()
        no_bank = groups.pop(self.env['res.partner.bank'], None)
        if no_bank:
            names = ', '.join(no_bank.mapped('employee_id.name'))
            raise UserError(_(
                'The following employees have no bank account and no '
                'run-level fallback:\n%s', names))
        no_type = [b for b in groups if not b.x_file_type]
        if no_type:
            accs = ', '.join(b.acc_number or str(b.id) for b in no_type)
            raise UserError(_(
                'These bank accounts have no Payroll File Type set:\n%s', accs))
        if require_type:
            groups = {b: s for b, s in groups.items() if b.x_file_type == require_type}
        return groups

    def _batch_label(self):
        return (self.run_id.name or '').replace(' ', '_').replace('/', '-')

    def _bank_label(self, bank):
        return (bank.acc_number or bank.bank_id.name or str(bank.id)
                ).replace(' ', '_').replace('/', '-')

    def _bundle_and_download(self, files):
        if not files:
            raise UserError(_('No files were generated.'))
        run = self.run_id
        if len(files) == 1:
            fname, data = files[0]
            mimetype = (
                'text/plain' if fname.endswith('.txt')
                else 'application/vnd.openxmlformats-officedocument'
                     '.spreadsheetml.sheet'
            )
            att = self.env['ir.attachment'].create({
                'name': fname, 'type': 'binary',
                'datas': base64.b64encode(data),
                'mimetype': mimetype,
                'res_model': run._name, 'res_id': run.id,
            })
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for fname, data in files:
                    zf.writestr(fname, data)
            att = self.env['ir.attachment'].create({
                'name': 'CommissionsBank_%s.zip' % self._batch_label(),
                'type': 'binary',
                'datas': base64.b64encode(buf.getvalue()),
                'mimetype': 'application/zip',
                'res_model': run._name, 'res_id': run.id,
            })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % att.id,
            'target': 'new',
        }

    # --- Employee identifiers ----------------------------------------------
    # The same reads as the payroll bank file (KSW_payroll
    # `_fill_wps_sheet` / `_build_kawthar_text`). This export used `barcode`
    # and `identification_id` instead: barcode is the biometric device ID and
    # identification_id is not where HR records the SSN (ssnid is), so both
    # columns came out blank.
    # And it printed `x_salary_bank_account_id` as the employee's account —
    # that is the COMPANY account the transfer leaves from, not the card or
    # IBAN it goes to (`primary_bank_account_id`).

    def _emp_number(self, emp):
        emp = emp.sudo()
        # x_employee_no is declared by KSW_payroll, which this module does
        # not depend on.
        number = emp.x_employee_no if 'x_employee_no' in emp._fields else ''
        return number or emp.barcode or ''

    def _emp_ssn(self, emp):
        emp = emp.sudo()
        return emp.ssnid or emp.identification_id or ''

    def _emp_account(self, emp):
        return emp.sudo().primary_bank_account_id

    def _sheet_title(self, prefix, bank, many):
        title = '%s %s' % (prefix, self._bank_label(bank)) if many else prefix
        for ch in '[]:*?/\\':
            title = title.replace(ch, '-')
        return title[:31]

    # --- Excel generation --------------------------------------------------

    def _make_comm_summary_excel(self, wb, lines):
        """Fill a summary worksheet (mirrors payroll summary but for commissions)."""
        ws = wb.active
        ws.title = 'Commission Summary'
        thin = Border(
            left=Side('thin'), right=Side('thin'),
            top=Side('thin'), bottom=Side('thin'),
        )
        bold = Font(bold=True, size=11)
        hdr_fill = PatternFill('solid', fgColor='D9E1F2')
        headers = [
            'Employee', 'Employee No', 'SSN No', 'Department',
            'Earnings', 'Loans Deduction', 'Bank Transfer Amount',
            'Bank Account Number', 'Bank Name', 'Paid From',
        ]
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.font = bold
            c.fill = hdr_fill
            c.border = thin
            c.alignment = Alignment(horizontal='center', wrap_text=True)
        for ri, line in enumerate(
                lines._export_sorted(), 2):
            emp = line.employee_id.sudo()
            bank = self._emp_account(emp)
            row = [
                emp.name or '',
                self._emp_number(emp),
                self._emp_ssn(emp),
                emp.department_id.name if emp.department_id else '',
                line.earnings,
                line.loan_offset,
                line.net_payable,
                bank.acc_number if bank else '',
                bank.bank_id.name if bank and bank.bank_id else '',
                line.bank_account_id.acc_number or '',
            ]
            for ci, v in enumerate(row, 1):
                c = ws.cell(row=ri, column=ci, value=v)
                c.border = thin
        for ci in range(1, len(headers) + 1):
            letter = openpyxl.utils.get_column_letter(ci)
            mx = max(len(str(ws.cell(row=r, column=ci).value or ''))
                     for r in range(1, ws.max_row + 1))
            ws.column_dimensions[letter].width = min(mx + 3, 35)

    def _fill_wps_sheet(self, wb, bank, lines, title):
        ws = wb.create_sheet(title)
        thin = Border(left=Side('thin'), right=Side('thin'),
                      top=Side('thin'), bottom=Side('thin'))
        bold = Font(bold=True, size=11)
        hdr_fill = PatternFill('solid', fgColor='C6EFCE')

        cic = bank.x_wps_cic_number or ''
        debit = bank.x_wps_debit_account or ''
        mol = bank.x_wps_mol_id or ''

        ws.cell(1, 1, 'CIC').font = bold
        ws.cell(1, 2, cic)
        ws.cell(2, 1, 'Debit Account:').font = bold
        ws.cell(2, 2, debit)
        ws.cell(3, 1, 'MOL ID').font = bold
        ws.cell(3, 2, mol)
        ws.cell(4, 1, 'Payment Purpose').font = bold
        ws.cell(4, 2, 'Commissions')

        en_headers = [
            'Bank Name', 'Account Number', 'Employee Name', 'Employee Number',
            'National ID', 'Amount (SAR)', 'Basic', 'HRA',
            'Other Earnings', 'Deductions', 'Department',
        ]
        for ci, h in enumerate(en_headers, 1):
            c = ws.cell(6, ci, h)
            c.font = bold
            c.fill = hdr_fill
            c.border = thin
            c.alignment = Alignment(horizontal='center')

        ri = 7
        for line in lines._export_sorted():
            amt = line.net_payable
            if not amt:
                continue
            emp = line.employee_id.sudo()
            emp_bank = self._emp_account(emp)
            row = [
                emp_bank.bank_id.name if emp_bank and emp_bank.bank_id else '',
                emp_bank.acc_number if emp_bank else '',
                emp.name or '',
                self._emp_number(emp),
                self._emp_ssn(emp),
                amt,
                line.earnings,
                0.0,  # HRA not applicable in commissions
                line.earnings,
                line.loan_offset,
                emp.department_id.name if emp.department_id else '',
            ]
            for ci, v in enumerate(row, 1):
                ws.cell(ri, ci, v).border = thin
            ri += 1
        for ci in range(1, len(en_headers) + 1):
            letter = openpyxl.utils.get_column_letter(ci)
            mx = max(len(str(ws.cell(row=r, column=ci).value or ''))
                     for r in range(6, max(ws.max_row + 1, 7)))
            ws.column_dimensions[letter].width = min(mx + 3, 40)

    def _make_wps_excel(self, groups):
        """One workbook: a summary over every line, one bank sheet per bank.

        :param groups: dict {paying res.partner.bank: ksw.pay.run.line}
        """
        if not openpyxl:
            raise UserError(_('openpyxl is required for Excel export.'))
        wb = openpyxl.Workbook()
        all_lines = self.env['ksw.pay.run.line']
        for lines in groups.values():
            all_lines |= lines
        self._make_comm_summary_excel(wb, all_lines)
        many = len(groups) > 1
        for bank, lines in groups.items():
            self._fill_wps_sheet(
                wb, bank, lines, self._sheet_title('WPS', bank, many))
        if 'Sheet' in wb.sheetnames and len(wb.sheetnames) > 1:
            del wb['Sheet']
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # --- TXT generation (Kawthar format) -----------------------------------

    @staticmethod
    def _pz(number, length):
        return str(int(number)).zfill(length)[:length]

    @staticmethod
    def _pr(text, length):
        return str(text or '')[:length].ljust(length)

    @staticmethod
    def _halalas(amount):
        return int(round((amount or 0.0) * 100))

    def _make_kawthar_txt(self, bank, lines):
        """Kawthar fixed-width 194-char TXT — the payroll layout, field for
        field (`KSW_payroll` `ksw.kawthar.file.wizard._build_kawthar_text`):

          Employee ID — 1..N over this file (12N) | CIC from card (10N) |
          Card number (14N) | Employee name (50A) | National ID (10N) |
          Net in halalas (15N) | Value date YYYYMMDD (8) | Operation (1X) |
          Zeros (6) | Spaces (20) | Basic (12N) | Housing (12N) |
          Other (12N) | Deductions (12N)

        The card is the EMPLOYEE's account (`primary_bank_account_id`),
        stored as CIC(5) + card number(14); `bank` is the company account
        the file is paid from and only decides which file a line lands in.
        """
        op = (self.operation_code or '2')[:1]
        vd = (self.value_date or fields.Date.context_today(self)).strftime('%Y%m%d')

        valid = lines.filtered(
            lambda l: self._emp_account(l.employee_id)
            and self._halalas(l.net_payable) > 0
        )._export_sorted()

        rows = []
        for seq, line in enumerate(valid, 1):
            emp = line.employee_id.sudo()
            acc_full = (self._emp_account(emp).acc_number or '').replace(' ', '')
            cic_from_card = acc_full[:5]
            card_no = acc_full[5:]
            nat_id = self._emp_ssn(emp)

            row = ''.join([
                self._pz(seq, 12),
                self._pz(int(cic_from_card) if cic_from_card.isdigit() else 0, 10),
                self._pr(card_no, 14),
                self._pr(emp.name, 50),
                self._pz(int(nat_id) if nat_id.isdigit() else 0, 10),
                self._pz(self._halalas(line.net_payable), 15),
                vd,
                op,
                '0' * 6,
                ' ' * 20,
                self._pz(self._halalas(line.earnings), 12),
                self._pz(0, 12),  # housing: not part of a commission
                self._pz(self._halalas(line.earnings), 12),
                self._pz(self._halalas(line.loan_offset), 12),
            ])
            assert len(row) == 194, f"Row length {len(row)} != 194"
            rows.append(row)

        return '\n'.join(rows)

    # ------------------------------------------------------------------
    # Main action
    # ------------------------------------------------------------------

    def action_export(self):
        self.ensure_one()
        mode = self.export_mode
        handlers = {
            'all_excel':     self._export_all_excel,
            'all_txt':       self._export_all_txt,
            'specific_excel': self._export_specific_excel,
            'specific_txt':  self._export_specific_txt,
            'journal_entry': self._export_journal_entry,
        }
        return handlers[mode]()

    def _export_journal_entry(self):
        """The month as one BAS journal entry, ready to import.

        No bank grouping: the entry is the month's posting, not a payment
        instruction, so it covers the whole register whichever bank each
        employee is paid from.
        """
        run = self.run_id
        # The writer (`ksw.bas.journal`) lives in KSW_payroll, which this
        # module deliberately does not depend on — adding the dependency
        # reorders module loading. So the model can genuinely be absent, and
        # it was on KSWCO the day this shipped: the commissions half of the
        # feature was deployed while the payroll half was not, and the
        # accountant got a bare KeyError traceback. Say what is wrong instead.
        if 'ksw.bas.journal' not in self.env:
            raise UserError(_(
                'The journal-entry export is not available on this system '
                'yet: it is written by KSW_payroll, which has not been '
                'updated. Ask IT to deploy it, or use one of the bank-file '
                'exports above.'))
        if not (self.journal_number or '').strip():
            raise UserError(_('Enter the journal number before exporting.'))
        data = run._bas_journal_workbook(number=self.journal_number)
        return self._bundle_and_download(
            [('JournalEntry_%s.xlsx' % self._batch_label(), data)])

    def _export_all_excel(self):
        groups = {
            b: lines for b, lines in self._group_and_validate().items()
            if b.x_file_type in ('wps', 'kawthar')
        }
        if not groups:
            raise UserError(_('No Excel files could be generated.'))
        bl = self._batch_label()
        if self.excel_layout == 'split':
            files = [
                ('Commissions_%s_%s.xlsx' % (bl, self._bank_label(bank)),
                 self._make_wps_excel({bank: lines}))
                for bank, lines in groups.items()
            ]
        else:
            files = [('Commissions_%s.xlsx' % bl, self._make_wps_excel(groups))]
        return self._bundle_and_download(files)

    def _export_all_txt(self):
        groups = self._group_and_validate(require_type='kawthar')
        files = []
        bl = self._batch_label()
        vd = (self.value_date or fields.Date.context_today(self)
              ).strftime('%Y%m%d')
        for bank, lines in groups.items():
            label = self._bank_label(bank)
            data = self._make_kawthar_txt(bank, lines).encode('utf-8')
            files.append(('Commissions_%s_%s_%s.txt' % (bl, label, vd), data))
        if not files:
            raise UserError(_('No text files could be generated.'))
        return self._bundle_and_download(files)

    def _export_specific_excel(self):
        if not self.bank_account_id:
            raise UserError(_('Please select a bank account.'))
        bank = self.bank_account_id
        groups = self._group_and_validate()
        lines = groups.get(bank)
        if not lines:
            raise UserError(_('No lines are assigned to the selected bank.'))
        bl = self._batch_label()
        label = self._bank_label(bank)
        data = self._make_wps_excel({bank: lines})
        return self._bundle_and_download(
            [('Commissions_%s_%s.xlsx' % (bl, label), data)])

    def _export_specific_txt(self):
        if not self.bank_account_id:
            raise UserError(_('Please select a bank account.'))
        if not self.value_date:
            raise UserError(_('Please set a value date for the text file.'))
        bank = self.bank_account_id
        groups = self._group_and_validate()
        lines = groups.get(bank)
        if not lines:
            raise UserError(_('No lines are assigned to the selected bank.'))
        bl = self._batch_label()
        label = self._bank_label(bank)
        vd = self.value_date.strftime('%Y%m%d')
        data = self._make_kawthar_txt(bank, lines).encode('utf-8')
        return self._bundle_and_download(
            [('Commissions_%s_%s_%s.txt' % (bl, label, vd), data)])

