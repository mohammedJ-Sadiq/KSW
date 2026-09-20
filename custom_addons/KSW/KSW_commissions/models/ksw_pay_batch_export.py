"""The batch, as a spreadsheet.

The counterpart of :mod:`ksw_pay_import_bas`: that one fills a batch from
BAS, this one hands the filled batch back out so it can be read, filed or
reconciled against the source it came from. Driver Trips is the case that
asked for it — sixty-seven imported lines nobody can check against BAS
inside a web form — but nothing here is import-specific, so every batch
exports the same way.

Two deliberate choices:

* **The columns follow the component, exactly as the form's columns do.**
  A Meals sheet has no Rate column and an Overtime sheet has no Free
  Allowance, for the same reason the screen does not show them: an empty
  column is a question the reader has to answer before he can ignore it.
* **Employee data is read with ``sudo()``.** A supervisor has no
  model-level read on ``hr.employee`` (his own searches are answered by
  ``hr.employee.public``), and ``x_employee_no`` / the BAS cost centre are
  ``hr.group_hr_user``-gated on top of that. Access to the batch has
  already been established by the time we get here — this is display, not
  a widening.
"""
import base64
import io
import re

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from odoo import _, fields, models
from odoo.exceptions import UserError

# Excel refuses these in a sheet name, and caps it at 31 characters.
_SHEET_NAME_BAD = re.compile(r'[\[\]:*?/\\]')


class KswPayBatchExport(models.Model):
    _inherit = 'ksw.pay.batch'

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------
    def action_export_xlsx(self):
        """Download this batch's entries as an .xlsx workbook."""
        self.ensure_one()
        if not self.entry_ids:
            raise UserError(_('There is nothing to export — this batch has '
                              'no entries yet.'))

        content = self._build_entries_workbook()
        attachment = self.env['ir.attachment'].create({
            'name': self._export_filename(),
            'type': 'binary',
            'datas': base64.b64encode(content),
            'mimetype': 'application/vnd.openxmlformats-officedocument.'
                        'spreadsheetml.sheet',
            # Deliberately unattached: an ir.attachment resolves access
            # through the record it points at, and several roles that may
            # read this batch may not read it again a moment later through
            # a different rule. With no res_model the creator keeps access
            # to what he just downloaded. Same reasoning as the deduction
            # statement export.
            'res_model': False,
            'res_id': False,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'new',
        }

    def _export_filename(self):
        self.ensure_one()
        scope = self.department_id.name or self.site_id.name or _('Company')
        period = self.period.strftime('%Y-%m') if self.period else ''
        name = '%s - %s - %s - %s' % (
            self.name or '', self.component_id.name or '', scope, period)
        return '%s.xlsx' % _SHEET_NAME_BAD.sub('-', name).strip(' -')

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------
    def _export_columns(self):
        """The columns this batch's component actually uses.

        Each is ``(header, kind, getter, total)`` where *kind* picks the
        cell format, *getter* takes one entry (and its employee, already
        sudo'd) and returns the value, and *total* says whether the column
        adds up. Not every number does: a driver's free allowance is a
        parameter of his own month, and a column of them summed is a
        figure that means nothing — the entry list on screen does not
        total it either.
        """
        self.ensure_one()
        component = self.component_id
        fixed = component.calculation == 'fixed'
        Employee = self.env['hr.employee']

        cols = [
            (_('Employee'), 'text', lambda e, emp: emp.name or '', False),
        ]
        # Only where the field exists — KSW_payroll is not a dependency.
        if 'x_employee_no' in Employee._fields:
            cols.append((_('Employee No.'), 'text',
                         lambda e, emp: emp.x_employee_no or '', False))
        # The one component-specific column, and it earns its place: the
        # cost centre is the key the BAS import matched on, so it is what
        # anybody checking these figures against BAS lines them up by.
        if self.importer == 'bas_trips':
            cols.append((_('BAS Cost Center'), 'text',
                         lambda e, emp: emp.x_bas_driver_cost_center or '',
                         False))
        cols.append((_('Department'), 'text',
                     lambda e, emp: emp.department_id.name or '', False))
        if component.has_options:
            cols.append((_('Type'), 'text',
                         lambda e, emp: e.option_id.name or '', False))
        if component.needs_date:
            cols.append((_('Date'), 'date', lambda e, emp: e.date, False))
        if component.qty_ref_label:
            cols.append((component.qty_ref_label, 'number',
                         lambda e, emp: e.quantity_ref, True))
        if not fixed:
            cols.append((component.qty_label or _('Quantity'), 'number',
                         lambda e, emp: e.quantity, True))
        if component.calculation == 'tiered':
            cols.append((_('Free Allowance'), 'number',
                         lambda e, emp: e.threshold_qty, False))
        if component.needs_location:
            cols.append((_('Location'), 'text',
                         lambda e, emp: e.location_id.name or '', False))
        cols.append((_('Reason'), 'text',
                     lambda e, emp: e.reason or '', False))
        cols.append((_('Further Details'), 'text',
                     lambda e, emp: e.details or '', False))
        if not fixed:
            cols.append((_('Rate'), 'rate', lambda e, emp: e.rate, False))
            cols.append((_('Computed Amount'), 'money',
                         lambda e, emp: e.amount_computed, True))
            cols.append((_('Override'), 'money',
                         lambda e, emp: e.amount_override or None, True))
        cols.append((_('Amount'), 'money', lambda e, emp: e.amount, True))
        return cols

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _build_entries_workbook(self):
        self.ensure_one()
        buf = io.BytesIO()
        book = xlsxwriter.Workbook(buf, {
            'in_memory': True,
            'default_date_format': 'yyyy-mm-dd',
        })
        fmt = self._export_formats(book)
        sheet = book.add_worksheet(self._export_sheet_name())
        if (self.env.lang or '').startswith('ar'):
            sheet.right_to_left()

        columns = self._export_columns()
        row = self._write_header(sheet, fmt, len(columns))
        head_row = row
        for index, (label, _kind, _get, _total) in enumerate(columns):
            sheet.write(row, index, label, fmt['head'])
        row += 1

        # sudo() once, in bulk: every entry's employee is read below and
        # the reader may have no hr.employee access of his own.
        entries = self.entry_ids.sorted(
            lambda e: (e.employee_id.sudo().name or '',
                       e.date or fields.Date.today(), e.id))
        first_data_row = row
        for entry in entries:
            employee = entry.employee_id.sudo()
            for index, (_label, kind, get, _total) in enumerate(columns):
                self._write_cell(sheet, row, index, get(entry, employee),
                                 kind, fmt)
            row += 1

        self._write_totals(sheet, fmt, columns, first_data_row, row)
        self._size_columns(sheet, columns, entries)
        sheet.freeze_panes(head_row + 1, 0)
        if row > first_data_row:
            sheet.autofilter(head_row, 0, row - 1, len(columns) - 1)

        book.close()
        return buf.getvalue()

    def _export_sheet_name(self):
        self.ensure_one()
        period = self.period.strftime('%b %Y') if self.period else ''
        name = '%s %s' % (self.component_id.name or _('Entries'), period)
        return _SHEET_NAME_BAD.sub('-', name).strip()[:31] or 'Entries'

    def _export_formats(self, book):
        money = '#,##0.00'
        return {
            'title': book.add_format({'bold': True, 'font_size': 14}),
            'label': book.add_format({'bold': True, 'align': 'left'}),
            'value': book.add_format({'align': 'left'}),
            'head': book.add_format({
                'bold': True, 'bg_color': '#D9E1F2', 'border': 1,
                'text_wrap': True, 'valign': 'vcenter'}),
            'text': book.add_format({'border': 1, 'valign': 'top'}),
            'date': book.add_format({
                'border': 1, 'num_format': 'yyyy-mm-dd'}),
            'number': book.add_format({'border': 1, 'num_format': money}),
            'rate': book.add_format({'border': 1, 'num_format': '#,##0.0000'}),
            'money': book.add_format({'border': 1, 'num_format': money}),
            'total_text': book.add_format({
                'bold': True, 'border': 1, 'bg_color': '#F2F2F2'}),
            'total_number': book.add_format({
                'bold': True, 'border': 1, 'bg_color': '#F2F2F2',
                'num_format': money}),
        }

    def _write_header(self, sheet, fmt, width):
        """The batch's own identity, above the table. Returns the next row."""
        self.ensure_one()
        sheet.merge_range(0, 0, 0, max(width - 1, 1),
                          self.display_name, fmt['title'])
        scope = self.department_id.name or self.site_id.name or _('Company')
        states = dict(self._fields['state']._description_selection(self.env))
        lines = [
            (_('Batch'), self.name or ''),
            (_('Component'), self.component_id.name or ''),
            (_('Scope'), scope),
            (_('Period'),
             self.period.strftime('%B %Y') if self.period else ''),
            (_('Status'), states.get(self.state, '')),
            (_('Exported by'), self.env.user.name),
            (_('Exported on'), fields.Datetime.to_string(
                fields.Datetime.context_timestamp(
                    self, fields.Datetime.now()))),
        ]
        row = 2
        for label, value in lines:
            sheet.write(row, 0, label, fmt['label'])
            sheet.write(row, 1, value, fmt['value'])
            row += 1
        return row + 1

    def _write_cell(self, sheet, row, col, value, kind, fmt):
        if value is None or value is False or value == '':
            sheet.write_blank(row, col, None, fmt.get(kind, fmt['text']))
        elif kind == 'date':
            sheet.write_datetime(row, col, value, fmt['date'])
        elif kind in ('number', 'money', 'rate'):
            sheet.write_number(row, col, float(value), fmt[kind])
        else:
            sheet.write_string(row, col, str(value), fmt['text'])

    def _write_totals(self, sheet, fmt, columns, first_row, row):
        """A totals row that adds itself up in Excel, not here.

        A formula rather than a stored figure so the row still agrees with
        the data after somebody filters or deletes a line in the copy he
        was sent.
        """
        if row <= first_row:
            return
        sheet.write(row, 0, _('Total'), fmt['total_text'])
        for index, (_label, _kind, _get, total) in enumerate(columns):
            if index == 0 or not total:
                if index:
                    sheet.write_blank(row, index, None, fmt['total_text'])
                continue
            col = xl_col_to_name(index)
            sheet.write_formula(
                row, index,
                '=SUM(%s%s:%s%s)' % (col, first_row + 1, col, row),
                fmt['total_number'])

    def _size_columns(self, sheet, columns, entries):
        """Widths from the widest cell, clamped — a free-text column of
        details must not push Amount off the screen."""
        for index, (label, _kind, get, _total) in enumerate(columns):
            width = len(label or '')
            for entry in entries[:200]:
                value = get(entry, entry.employee_id.sudo())
                if value not in (None, False):
                    width = max(width, len(str(value)))
            sheet.set_column(index, index, min(max(width + 2, 10), 40))
