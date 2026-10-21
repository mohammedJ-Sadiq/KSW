"""Check a posted file against what BAS actually holds.

Odoo records what it produced; BAS holds what was imported. Between the two
sits an Excel file anyone can edit, and neither system notices: August 2026
reached BAS (26008282) with three Driver Trips lines raised by 500 and two
blocks typed by hand, none of it in any file Odoo produced, and it surfaced
only weeks later as reversals nobody could explain.

So after a file is imported, its voucher number is read back from BAS
(read-only, KSW_ext_sync's connector) and compared line by line — account,
side, amount, description — with the file Odoo produced. The result is kept
on the voucher, with a chatter note, and a daily job keeps checking recent
files until they match.
"""
import base64
import io
import logging
from collections import Counter
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

_logger = logging.getLogger(__name__)

# How long after posting the daily job keeps checking a file.
CHECK_WINDOW_DAYS = 45


def _norm(text):
    """One spelling for a description: whitespace collapsed, alef forms
    folded (اغسطس / أغسطس), the padding BAS's own export adds stripped."""
    text = ' '.join(str(text or '').split())
    for alef in 'أإآ':
        text = text.replace(alef, 'ا')
    return text


def _amount(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


class KswPaySettlement(models.Model):
    _inherit = 'ksw.pay.settlement'

    bas_check_state = fields.Selection(
        [('not_checked', 'Not Checked'),
         ('missing', 'Not in BAS Yet'),
         ('match', 'Matches BAS'),
         ('differs', 'Differs from BAS')],
        string='BAS Check', default='not_checked', readonly=True, copy=False,
        tracking=True,
        help='The last time this file was compared with the voucher of the '
             'same number in BAS.')
    bas_check_date = fields.Datetime(
        string='Checked On', readonly=True, copy=False)
    bas_check_by_id = fields.Many2one(
        'res.users', string='Checked By', readonly=True, copy=False)
    bas_check_diff_count = fields.Integer(
        string='Differences', readonly=True, copy=False)
    bas_check_report = fields.Text(
        string='Comparison with BAS', readonly=True, copy=False)

    # ------------------------------------------------------------------
    # The two sides
    # ------------------------------------------------------------------
    def _file_lines(self):
        """``Counter((code, side, amount, description))`` — the file Odoo
        produced, as it is attached to the voucher."""
        self.ensure_one()
        if not openpyxl:
            raise UserError(_('openpyxl is required to read the file.'))
        if not self.attachment_id:
            raise UserError(_('This voucher has no file to compare.'))
        sheet = openpyxl.load_workbook(io.BytesIO(
            base64.b64decode(self.attachment_id.sudo().datas))).active
        lines = Counter()
        # Row 1 empty, headers on row 2 (ksw.bas.journal.build_workbook).
        for row in sheet.iter_rows(min_row=3, values_only=True):
            code = str(row[2] or '').strip()
            if not code:
                continue
            debit, credit = _amount(row[4]), _amount(row[5])
            lines[(code, 'Dr' if debit else 'Cr', debit or credit,
                   _norm(row[6]))] += 1
        return lines

    def _bas_voucher_rows(self):
        """VOU10 rows of the voucher with this file's number (read-only)."""
        self.ensure_one()
        number = (self.journal_number or '').strip()
        if not number.isdigit():
            raise UserError(_(
                'This voucher has no BAS journal number to look up.'))
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(
                "SELECT SERIAL, FCODE, TCODE, AMOUNT, REF FROM VOU10 "
                "WHERE NUMBER1 = %s AND CODE2 = '010' ORDER BY SERIAL",
                (int(number),))
            return cur.fetchall()
        finally:
            conn.close()

    @api.model
    def _bas_lines(self, rows):
        lines = Counter()
        for row in rows:
            if row['FCODE']:
                code, side = row['FCODE'], 'Dr'
            else:
                code, side = row['TCODE'], 'Cr'
            lines[(str(code or '').strip(), side, _amount(row['AMOUNT']),
                   _norm(row['REF']))] += 1
        return lines

    # ------------------------------------------------------------------
    # The comparison
    # ------------------------------------------------------------------
    @api.model
    def _compare(self, odoo, bas):
        """``{'changed': [(key, odoo amt, bas amt)], 'added': [...],
        'other': [...], 'missing': [...]}``.

        A line on the same account, side and description with another
        amount is *changed*; one BAS has and the file does not is *added* —
        on this file's own accounts, or on others (a sales commission typed
        into the same voucher); one the file has and BAS does not is
        *missing*.
        """
        only_bas = list((bas - odoo).elements())
        only_odoo = list((odoo - bas).elements())
        changed = []
        for key in list(only_bas):
            code, side, amount, ref = key
            twin = next((k for k in only_odoo
                         if k[0] == code and k[1] == side and k[3] == ref),
                        None)
            if twin:
                changed.append((key, twin[2], amount))
                only_bas.remove(key)
                only_odoo.remove(twin)
        own_accounts = {k[0] for k in odoo}
        return {
            'changed': changed,
            'added': [k for k in only_bas if k[0] in own_accounts],
            'other': [k for k in only_bas if k[0] not in own_accounts],
            'missing': only_odoo,
        }

    def _report(self, result, odoo, bas):
        def line(key, amount=None):
            code, side, amt, ref = key
            return '  • %s %s %s | %s' % (
                code, side, '%.2f' % (amt if amount is None else amount), ref)

        parts = [
            _('BAS voucher %(number)s: %(bas)s line(s); file: %(odoo)s line(s); '
              '%(same)s identical.',
              number=self.journal_number, bas=sum(bas.values()),
              odoo=sum(odoo.values()), same=sum((bas & odoo).values())),
        ]
        if result['changed']:
            parts += ['', _('Changed in BAS (file → BAS):')]
            parts += ['  • %s %s %.2f → %.2f | %s' % (
                k[0], k[1], was, now, k[3]) for k, was, now in result['changed']]
        if result['added']:
            parts += ['', _('Added in BAS on this file\'s accounts:')]
            parts += [line(k) for k in result['added']]
        if result['missing']:
            parts += ['', _('In the file but not in BAS:')]
            parts += [line(k) for k in result['missing']]
        if result['other']:
            parts += ['', _('Also in the BAS voucher, on accounts this file '
                            'does not use (e.g. sales commission typed in '
                            'the same voucher):')]
            parts += [line(k) for k in result['other']]
        return '\n'.join(parts)

    def _check_against_bas(self):
        """Compare and record. Returns the state."""
        self.ensure_one()
        odoo = self._file_lines()
        rows = self._bas_voucher_rows()
        if not rows:
            state, count = 'missing', 0
            report = _('BAS has no voucher %(number)s yet.',
                       number=self.journal_number)
        else:
            bas = self._bas_lines(rows)
            result = self._compare(odoo, bas)
            # Lines on other accounts are reported, not counted: a voucher
            # BAS shares with another commission is not this file being
            # wrong.
            count = (len(result['changed']) + len(result['added'])
                     + len(result['missing']))
            state = 'differs' if count else 'match'
            report = self._report(result, odoo, bas)
        changed = (state != self.bas_check_state
                   or report != (self.bas_check_report or ''))
        self.sudo().write({
            'bas_check_state': state,
            'bas_check_date': fields.Datetime.now(),
            'bas_check_by_id': self.env.uid,
            'bas_check_diff_count': count,
            'bas_check_report': report,
        })
        if changed:
            title = dict(self._fields['bas_check_state']._description_selection(
                self.env)).get(state, state)
            self.sudo().message_post(
                body=Markup('<strong>%(title)s</strong><br/>%(text)s') % {
                    'title': _('BAS check: %(state)s', state=title),
                    'text': Markup('<br/>').join(report.split('\n')[:40]),
                }, subtype_xmlid='mail.mt_note')
        return state

    def action_check_against_bas(self):
        for rec in self:
            rec._check_accountant()
            if rec.state != 'done' or not rec.attachment_id:
                raise UserError(_(
                    'Only a posted voucher with a file can be checked '
                    'against BAS.'))
            rec._check_against_bas()
        return True

    @api.model
    def _cron_check_against_bas(self, commit=True):
        """Keep checking recent files until BAS holds them as produced.

        Commits after each voucher (a BAS connection that drops halfway
        keeps what was checked); ``commit=False`` uses savepoints instead.
        """
        since = fields.Datetime.now() - timedelta(days=CHECK_WINDOW_DAYS)
        vouchers = self.sudo().search([
            ('state', '=', 'done'),
            ('attachment_id', '!=', False),
            ('journal_number', '!=', False),
            ('done_date', '>=', since),
            ('bas_check_state', '!=', 'match'),
        ])
        testing = not commit
        for voucher in vouchers:
            try:
                if testing:
                    with self.env.cr.savepoint():
                        voucher._check_against_bas()
                else:
                    voucher._check_against_bas()
                    self.env.cr.commit()
            except Exception:  # noqa: BLE001 — one bad voucher must not stop the rest
                if not testing:
                    self.env.cr.rollback()
                _logger.exception('BAS check failed for %s', voucher.name)
