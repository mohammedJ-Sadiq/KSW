"""The one importer: driver trips from BAS.

Oracle calls this a batch loader — a way to fill a batch from an outside
system. It is the only genuinely type-specific code left in the entry side,
and it is an *importer*, not an application: about forty lines hanging off a
button, dispatched by name from the component's ``importer`` field.

The BAS query moved here verbatim when the old driver-commission model was
retired: item 11032 water loads from ``vou10``/``STR10``, weighted by each
destination customer's distance factor (``cod10.FACTORE``), matched to
employees through ``hr.employee.x_bas_driver_cost_center``. Configurable via
``ir.config_parameter`` ``ksw_commissions.orood_item_codes`` (default 11032)
and ``ksw_commissions.orood_ftypes`` (default 600).
"""
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KswPayBatchBasImport(models.Model):
    _inherit = 'ksw.pay.batch'

    def _import_bas_trips(self):
        """Fill this batch with each site driver's trips for the month.

        The quantity is الرد المضاعف (loads weighted by each destination's
        distance factor) and the free allowance is the driver's required
        trips pro-rated to the days he actually worked — both of which the
        tiered resolver then turns into money.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Import is only available while the batch is in Draft."))
        if not self.site_id:
            raise UserError(_(
                "Select a Work Site on the batch before importing."))

        date_from = self.period.replace(day=1)
        date_to = date_from + relativedelta(months=1)

        employees = self.env['hr.employee'].search([
            ('x_site_id', '=', self.site_id.id),
        ])
        if not employees:
            raise UserError(_(
                "No employees are assigned to %(site)s.",
                site=self.site_id.name))

        bas_data = self._bas_fetch_orood(date_from, date_to)
        existing = {e.employee_id.id: e for e in self.entry_ids}

        matched = 0
        unmapped = self.env['hr.employee']
        no_data = self.env['hr.employee']
        kept = self.env['hr.employee']
        commands = []
        for employee in employees:
            cost_center = (employee.x_bas_driver_cost_center or '').strip()
            data = bas_data.get(self._norm_cc(cost_center)) \
                if cost_center else None

            entry = existing.get(employee.id)
            if not data:
                # Nothing to import for this driver. Writing a zero-quantity
                # entry is not an option — ksw.pay.entry._check_quantity
                # rejects it and the ValidationError would roll back the whole
                # import, so a single unmapped driver would cost every other
                # driver his trips and report nobody by name. Skip him and say
                # so in the summary instead.
                if not cost_center:
                    unmapped |= employee
                else:
                    no_data |= employee
                # A line already in the batch is left alone: it may hold a
                # figure someone entered or reviewed, and this importer cannot
                # tell. It is named in the summary so it gets a second look.
                if entry:
                    kept |= employee
                continue

            worked = self._get_worked_days_from_sheet(employee, self.period)
            required = self._bas_required_trips(worked)
            vals = {
                # الرد المضاعف — what the tiers are calculated on.
                'quantity': data['mult'],
                # عدد الردود — the raw count, kept so the amount can be
                # justified at review rather than appearing from nowhere.
                'quantity_ref': float(data['loads']),
                'threshold_qty': required,
                'details': _(
                    'Worked days: %(days)s. Required trips before earning: '
                    '%(required).0f.',
                    days=worked if worked is not None else _('not recorded'),
                    required=required),
            }
            matched += 1
            if entry:
                commands.append((1, entry.id, vals))
            else:
                commands.append((0, 0, dict(vals, employee_id=employee.id)))

        if commands:
            self.write({'entry_ids': commands})

        message = _(
            "%(matched)s driver(s) filled from BAS.", matched=matched)
        if unmapped:
            message += _(
                "\n%(count)s have no BAS cost centre set: %(names)s",
                count=len(unmapped),
                names=self._name_list(unmapped))
        if no_data:
            message += _(
                "\n%(count)s had no trips in BAS this month: %(names)s",
                count=len(no_data),
                names=self._name_list(no_data))
        if kept:
            message += _(
                "\n%(count)s already had a line in this batch and were left "
                "unchanged: %(names)s",
                count=len(kept),
                names=self._name_list(kept))
        self.sudo().message_post(body=message, subtype_xmlid='mail.mt_note')
        return self._notify(message, title=_('Imported from BAS'))

    @staticmethod
    def _name_list(employees, limit=5):
        """First few employee names, with a count of the rest."""
        names = ', '.join(employees[:limit].mapped('name'))
        if len(employees) > limit:
            names += _(' and %(more)s more', more=len(employees) - limit)
        return names

    def _bas_required_trips(self, worked_days):
        """The free allowance: required trips pro-rated to days worked.

        Mirrors the old ``round(site.required_trips_full_month * worked / 30)``
        so historical figures reproduce exactly.
        """
        self.ensure_one()
        base = self.site_id.required_trips_full_month or 0
        if worked_days is None:
            return float(base)
        return float(round(base * (worked_days or 0) / 30.0))

    @staticmethod
    def _norm_cc(value):
        """Normalise a driver cost-center string for tolerant matching.

        BAS stores «مركز تكلفة الموظف» with non-breaking spaces (U+00A0),
        inconsistent casing and stray whitespace, so exact comparison misses.
        Replace NBSP with a space, collapse runs of whitespace, and lowercase.
        """
        if not value:
            return ''
        return ' '.join(value.replace('\xa0', ' ').split()).lower()

    def _bas_fetch_orood(self, date_from, date_to):
        """Query BAS for per-driver عدد الردود / الرد المضاعف.

        Returns ``{normalised_cost_center: {'loads': int, 'mult': float,
        'equip': str, 'raw_cc': str}}`` for every driver with at least one
        water-load line in the half-open range ``[date_from, date_to)``.
        Matching is done on the normalised key (see _norm_cc); the caller
        looks drivers up by ``_norm_cc(employee.x_bas_driver_cost_center)``.
        """
        result = {}
        ICP = self.env['ir.config_parameter'].sudo()
        items = [c.strip() for c in (ICP.get_param(
            'ksw_commissions.orood_item_codes', '11032') or '').split(',')
            if c.strip()]
        ftypes = [c.strip() for c in (ICP.get_param(
            'ksw_commissions.orood_ftypes', '600') or '').split(',')
            if c.strip()]
        if not items or not ftypes:
            return result

        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            item_ph = ','.join(['%s'] * len(items))
            ft_ph = ','.join(['%s'] * len(ftypes))
            sql = (
                "SELECT h.COST_CENTER2 AS driver_cc, "
                "       COUNT(*) AS loads, "
                "       SUM(ISNULL(c.FACTORE, 0)) AS mult, "
                "       MAX(h.COST_CENTER) AS equip "
                "FROM vou10 h "
                "JOIN STR10 s ON s.FTYPE=h.FTYPE AND s.FTYPE2=h.FTYPE2 "
                "            AND s.CODE2=h.CODE2 AND s.NUMBER1=h.NUMBER1 "
                "LEFT JOIN cod10 c ON c.DCODE1 = s.FCODE "
                "WHERE h.FTYPE IN (" + ft_ph + ") "
                "  AND s.ICODE IN (" + item_ph + ") "
                "  AND h.FDATE >= %s AND h.FDATE < %s "
                "  AND h.COST_CENTER2 IS NOT NULL AND h.COST_CENTER2 <> '' "
                "GROUP BY h.COST_CENTER2"
            )
            params = tuple(
                ftypes + items
                + [fields.Date.to_string(date_from),
                   fields.Date.to_string(date_to)])
            cur.execute(sql, params)
            for row in cur.fetchall():
                raw = row['driver_cc'] or ''
                key = self._norm_cc(raw)
                if not key:
                    continue
                loads = int(row['loads'] or 0)
                mult = float(row['mult'] or 0.0)
                if key in result:
                    # Two raw spellings collapse to the same driver — merge.
                    result[key]['loads'] += loads
                    result[key]['mult'] += mult
                else:
                    result[key] = {
                        'loads': loads, 'mult': mult,
                        'equip': (row['equip'] or '').strip(),
                        'raw_cc': raw.replace('\xa0', ' ').strip(),
                    }
        finally:
            conn.close()
        return result

    def _get_worked_days_from_sheet(self, employee, period):
        """Attended days from the employee's monthly attendance sheet.

        Returns the sheet's ``total_attended`` for the period's month, or
        ``None`` when no sheet exists (so the caller leaves ``worked_days``
        untouched rather than zeroing it). Prefers a confirmed sheet.
        """
        Sheet = self.env['ksw.attendance.sheet'].sudo()
        domain = [
            ('employee_id', '=', employee.id),
            ('month', '=', str(period.month)),
            ('year', '=', period.year),
        ]
        sheet = Sheet.search(domain + [('state', '=', 'confirmed')], limit=1) \
            or Sheet.search(domain, limit=1)
        return sheet.total_attended if sheet else None
