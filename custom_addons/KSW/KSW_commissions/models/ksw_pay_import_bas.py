"""The one importer: driver trips from BAS.

Oracle calls this a batch loader — a way to fill a batch from an outside
system. It is the only genuinely type-specific code left in the entry side,
and it is an *importer*, not an application: about forty lines hanging off a
button, dispatched by name from the component's ``importer`` field.

The BAS query moved here verbatim when the old driver-commission model was
retired: item 11032 water loads from ``vou10``/``STR10``, matched to
employees through ``hr.employee.x_bas_driver_cost_center`` — the drivers
being whoever sits under the batch's department. Configurable via
``ir.config_parameter`` ``ksw_commissions.orood_item_codes`` (default 11032)
and ``ksw_commissions.orood_ftypes`` (default 600).

**The weighting moved from the customer master to the invoice line, and
the move is dated.** It used to be ``cod10.FACTORE`` — «الرد المضاعف», the
destination's *current* distance multiplier. That is a live value: changing
a customer's ratio silently rewrote every month ever imported from it,
including months already paid, because the old figure was nowhere on the
document. From ``INVOICE_FACTOR_FROM_DEFAULT`` it reads «رد الفاتورة»
(``STR10.TAXES_5``), the multiplier as it stood **when the invoice was
issued**, so a ratio change takes effect from the day it is set.

**Before that date the old basis stands.** A changeover is a date, not a
switch: August has always been costed on «الرد المضاعف» and is about to be
paid, so re-basing it the same week would move a figure the supervisors have
already reviewed — the exact retroactive move this change exists to stop.
The choice is made **per invoice line, by document date**, so a month
straddling the cutover splits at the right day.

``TAXES_5`` is a legacy tax column BAS reuses for this; the name means
nothing here. Confirmed against «الحركة التجارية للأصناف» on document
172/9026417 (20 Aug 2026), where the line reads 2.5 while the customer master
had since been moved to 2.0 — exactly the drift this change removes.

A line with **no** «رد الفاتورة» is not weightless, it is unrecorded, so it is
never silently counted as zero: a driver with nothing at all is skipped and
named, and a driver with some missing lines is imported with a warning
saying how many. Only lines on the **new** side of the cutover can be
"missing" — a pre-cutover line has nothing to miss.

⚠️ **BAS stopped writing the column on 6 Sep 2026** — ~99% of lines carry it
from January to August, then 0% from 6 September. Until that is fixed on the
BAS side, any period on the new basis imports next to nothing, and every
driver lands in the batch's Not Imported log saying so.
"""
import calendar
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, fields, models
from odoo.exceptions import UserError

from .ksw_vacation_hold import vacation_holds

_logger = logging.getLogger(__name__)

#: «رد الفاتورة» — the destination's distance multiplier as it stood
#: when the invoice was issued. A legacy tax column BAS reuses for it,
#: so the name means nothing; the values are the ordinary ladder
#: (0.75, 1, 1.25, 1.5, 2, 2.14, 2.5, 3, 6). A constant, never user
#: input — it is interpolated into SQL.
INVOICE_FACTOR_COLUMN = 'TAXES_5'

#: The day the new basis takes effect. Loads invoiced **before** it keep
#: «الرد المضاعف» (`cod10.FACTORE`, the customer's current multiplier);
#: from it on they use «رد الفاتورة».
#:
#: A changeover is a date, not a switch: a month that has always been paid
#: one way must not be re-based the week it is about to be paid, however
#: much better the new basis is. Judged **per invoice line, by document
#: date**, so a month straddling the cutover is split at the right day
#: rather than rounded to whichever basis the period starts on.
#:
#: Override with `ir.config_parameter` `ksw_commissions.invoice_factor_from`
#: (YYYY-MM-DD). Set it to a past date to re-base history, or far in the
#: future to stay entirely on the old basis.
INVOICE_FACTOR_FROM_DEFAULT = '2026-09-06'


class KswPayBatchBasImport(models.Model):
    _inherit = 'ksw.pay.batch'

    def _import_bas_trips(self):
        """Fill this batch with each of the department's drivers' trips.

        Whose trips to fetch used to be a work-site question — the batch
        named a site and the drivers were whoever carried it on their
        employee record. Two registers to keep in step (the site list and
        every driver's assignment) for a question the batch already
        answers: it is recorded by the supervisor, for his department.

        So the pool is simply :meth:`_allowed_employees` — everyone in the
        batch's department plus anyone who reports to this supervisor from
        elsewhere. That is deliberately the *same* pool the employee picker
        on the form uses, so the importer can never produce a line the
        picker would have refused. Whoever has no BAS cost centre, or no
        loads this month, is named in the summary rather than skipped
        silently.

        The quantity is Σ «رد الفاتورة» — each load weighted by the
        destination's distance factor **as recorded on that invoice**, not
        by the customer's current one — and the free allowance is the
        driver's required trips pro-rated to the days he actually worked.
        The tiered resolver turns both into money.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Import is only available while the batch is in Draft."))
        if not self.department_id:
            raise UserError(_(
                "Select a Department on the batch before importing — the "
                "drivers are taken from it."))

        date_from = self.period.replace(day=1)
        date_to = date_from + relativedelta(months=1)

        employees = self._allowed_employees()
        if not employees:
            raise UserError(_(
                "There are no employees under %(department)s.",
                department=self.department_id.name))

        bas_data = self._bas_fetch_orood(date_from, date_to)
        existing = {e.employee_id.id: e for e in self.entry_ids}

        # A driver who was on vacation this month was paid for it on the
        # leave. Importing his loads anyway would pay them a second time,
        # and it is BAS that hands us the trap: the loads are booked to his
        # cost centre whoever actually drove the truck while he was away.
        cutover = self._invoice_factor_cutover()
        holds = vacation_holds(self.env, employees, self.period)
        # He came back mid-month: only the days from his return date on are
        # his. One extra BAS query per distinct return date, not per driver.
        windows = {}
        for employee_id, hold in holds.items():
            if hold.kind == 'partial':
                windows.setdefault(hold.payable_from, {})
        for payable_from in list(windows):
            windows[payable_from] = self._bas_fetch_orood(
                payable_from, date_to)

        matched = 0
        unmapped = self.env['hr.employee']
        no_data = self.env['hr.employee']
        kept = self.env['hr.employee']
        on_vacation = self.env['hr.employee']
        part_month = self.env['hr.employee']
        no_attendance = self.env['hr.employee']
        no_factor = self.env['hr.employee']
        part_factor = self.env['hr.employee']
        cash_rerated = self.env['hr.employee']
        out_of_scope = self.env['hr.employee']
        commands = []
        # Rebuilt from scratch every import: the log describes THIS run,
        # and a stale row from a run whose cause has since been fixed is
        # worse than no row at all.
        Skip = self.env['ksw.pay.batch.skip.line'].sudo()
        self.skip_line_ids.sudo().unlink()
        log = []
        for employee in employees:
            hold = holds.get(employee.id)
            # `!= 'partial'`, NOT `== 'full'`: a part month is the only
            # kind with a date to work from. Testing for one specific
            # blocking kind meant the next one added ('settled_later')
            # fell straight through to windows[None] — KeyError in the
            # supervisor's face, on the live Import button.
            if hold and hold.kind != 'partial':
                # Not "no data" — he has loads in BAS, they are just not
                # his to be paid for. Logged on the batch so nobody has to
                # wonder why the department came up one driver short.
                on_vacation |= employee
                note = _(
                    'Went on %(type)s on %(start)s. %(why)s',
                    type=hold.leave.holiday_status_id.sudo().display_name,
                    start=fields.Date.to_string(
                        hold.leave.request_date_from),
                    why=(_('This month was still owed to him then, so it '
                           'went out with that settlement.')
                         if hold.kind == 'settled_later'
                         else _('The month was settled on that request.')),
                )
                if employee.id in existing:
                    note += _(
                        ' A line from an earlier import is still on this '
                        'batch and will not be paid — delete it.')
                log.append(Skip._log(self, employee, 'on_vacation', note))
                continue
            cost_center = ', '.join(
                v.strip() for v in
                ([employee.x_bas_driver_cost_center or '']
                 + (employee.x_bas_driver_cost_center_alt or '').split(','))
                if v.strip())
            source = bas_data
            if hold:
                # Built up front for the whole batch; fetched here if some
                # future path produces a hold the prepass never saw, so a
                # missing key can never again cost the supervisor his
                # import.
                source = windows.get(hold.payable_from)
                if source is None:
                    source = windows[hold.payable_from] = \
                        self._bas_fetch_orood(hold.payable_from, date_to)
                part_month |= employee
                log.append(Skip._log(
                    self, employee, 'part_month',
                    _('Back from %(type)s on %(date)s — only loads from '
                      'that date onwards were counted. The earlier part '
                      'of the month was settled on the leave.',
                      type=hold.leave.holiday_status_id.sudo().display_name,
                      date=fields.Date.to_string(hold.payable_from))))
            data = self._bas_merge(source, self._bas_cost_centres(employee))

            entry = existing.get(employee.id)
            if not data:
                # Nothing to import for this driver. Writing a zero-quantity
                # entry is not an option — ksw.pay.entry._check_quantity
                # rejects it and the ValidationError would roll back the whole
                # import, so a single unmapped driver would cost every other
                # driver his trips and report nobody by name. Skip him and say
                # so in the summary instead.
                if not self._bas_cost_centres(employee):
                    unmapped |= employee
                    log.append(Skip._log(
                        self, employee, 'no_cost_centre',
                        _('No BAS Driver Cost Center on the employee '
                          'record, so his loads cannot be matched. Set it '
                          'on the employee and import again.')))
                else:
                    no_data |= employee
                    log.append(Skip._log(
                        self, employee, 'no_data',
                        _('BAS has no water loads booked to "%(cc)s" for '
                          '%(period)s.', cc=cost_center,
                          period=self.period.strftime('%B %Y'))))
                # A line already in the batch is left alone: it may hold a
                # figure someone entered or reviewed, and this importer cannot
                # tell. It is named in the summary so it gets a second look.
                if entry:
                    kept |= employee
                    log.append(Skip._log(
                        self, employee, 'kept',
                        _('A line was already on this batch and was left '
                          'untouched, because this import cannot tell '
                          'whether somebody reviewed it.')))
                continue

            # The truck, kept for the journal entry's cost-centre
            # column. BAS gives it per month, on the loads themselves —
            # there is nowhere else to get it, and the hand-typed voucher
            # carries it on every commission line.
            equipment = (data.get('equip') or '').strip()
            if equipment and employee.sudo().x_bas_equipment_code != equipment:
                employee.sudo().x_bas_equipment_code = equipment

            # A load with no «رد الفاتورة» is not weightless, it is
            # unrecorded. Counting it as zero would quietly shrink the
            # driver's month; BAS stopped filling the column on 6 Sep 2026,
            # so this is the difference between "he did less" and "nobody
            # wrote it down".
            if not data['mult']:
                no_factor |= employee
                log.append(Skip._log(
                    self, employee, 'no_invoice_factor',
                    _('BAS recorded %(loads)d load(s) for him in '
                      '%(period)s but there is no weighting on any of them '
                      '(%(new)d invoiced on or after %(cut)s carry no '
                      '«رد الفاتورة»), so there is nothing to pay against. '
                      'This is a BAS data problem, not an absence — his '
                      'loads are there.',
                      loads=data['loads'],
                      new=data.get('new_basis') or 0,
                      cut=fields.Date.to_string(cutover),
                      period=self.period.strftime('%B %Y'))))
                if entry:
                    kept |= employee
                continue
            if data.get('missing'):
                part_factor |= employee
                log.append(Skip._log(
                    self, employee, 'part_invoice_factor',
                    _('%(missing)d of the %(new)d load(s) invoiced on or '
                      'after %(cut)s carry no «رد الفاتورة», so they are '
                      'not counted in the weighted quantity below. Check '
                      'the figure before approving.',
                      missing=data['missing'],
                      new=data.get('new_basis') or 0,
                      cut=fields.Date.to_string(cutover))))

            # Re-rated, never silently. The till's own rate is what the
            # line carries in BAS; the band is what the business says the
            # run was worth. Saying so on the record is the whole point —
            # the figure differs from BAS and the reason has to travel
            # with it.
            if data.get('cash_loads'):
                cash_delta = round(data['cash_at_band']
                                   - data['cash_at_till'], 2)
                if abs(cash_delta) > 0.005:
                    cash_rerated |= employee
                    log.append(Skip._log(
                        self, employee, 'cash_band',
                        _('%(n)d load(s) went to cash customers. BAS books '
                          'those to the till, whose own rate would weight '
                          'them %(till).2f; the amount bands weight them '
                          '%(band).2f — a difference of %(d)+.2f on this '
                          'driver.',
                          n=data['cash_loads'], till=data['cash_at_till'],
                          band=data['cash_at_band'], d=cash_delta)))

            worked = self._get_worked_days_from_sheet(employee, self.period)
            required = self._bas_required_trips(worked, self.period)
            if worked is None:
                # Charged the FULL month's requirement, because nothing
                # says otherwise. Never silently: that is the one outcome
                # the driver cannot argue with and cannot see.
                no_attendance |= employee
                log.append(Skip._log(
                    self, employee, 'no_attendance',
                    _('No attendance sheet and no punches for %(period)s, '
                      'so the full %(req).0f required trips were applied '
                      'instead of a pro-rated figure. Check his sheet '
                      'before approving.',
                      period=self.period.strftime('%B %Y'),
                      req=required)))
            vals = {
                # Σ «رد الفاتورة» — what the tiers are calculated on.
                'quantity': data['mult'],
                # عدد الردود — the raw count, kept so the amount can be
                # justified at review rather than appearing from nowhere.
                'quantity_ref': float(data['loads']),
                'threshold_qty': required,
                'details': _(
                    'Weighted on %(basis)s. Worked days: %(days)s. '
                    'Required trips before earning: %(required).0f.',
                    basis=(
                        _('«رد الفاتورة» as invoiced')
                        if (data.get('new_basis') or 0) >= data['loads']
                        else (_('«الرد المضاعف» (customer rate)')
                              if not data.get('new_basis')
                              else _('both bases, split at %(cut)s',
                                     cut=fields.Date.to_string(cutover)))),
                    days=worked if worked is not None else _('not recorded'),
                    required=required),
            }
            if data.get('missing'):
                # The figure is short and the entry itself must say so —
                # the import log is on another tab, and this line is what
                # the approver reads next to the money.
                vals['details'] += _(
                    ' Only %(counted)d of %(new)d load(s) invoiced from '
                    '%(cut)s are weighted; the other %(missing)d carry no '
                    '«رد الفاتورة» in BAS.',
                    counted=(data.get('new_basis') or 0) - data['missing'],
                    new=data.get('new_basis') or 0,
                    missing=data['missing'],
                    cut=fields.Date.to_string(cutover))
            if hold:
                vals['details'] += _(
                    ' Back from vacation on %(date)s — only loads from that '
                    'date onwards are counted; the earlier part of the '
                    'month was settled on the leave.',
                    date=fields.Date.to_string(hold.payable_from))
            matched += 1
            if entry:
                commands.append((1, entry.id, vals))
            else:
                commands.append((0, 0, dict(vals, employee_id=employee.id)))

        if commands:
            self.write({'entry_ids': commands})

        # Entries for people the importer never looks at. `employees` is the
        # batch's scope, so an entry outside it is walked past in silence and
        # keeps whatever figure was last written — for weeks, if nobody
        # notices. It is still paid. Usually the employee has lost their
        # department, which takes them out of scope without touching the
        # entry that is already here (KSWCO, Aug 2026: four drivers, one of
        # them earning 0.00 on a stale figure that sat below the threshold
        # while his real one was above it).
        stranded = self.entry_ids.employee_id - employees
        for employee in stranded:
            out_of_scope |= employee
            log.append(Skip._log(
                self, employee, 'out_of_scope',
                _('This entry is for somebody outside %(scope)s who does '
                  'not report to this batch, so the import cannot refresh '
                  'it — the figure shown is whatever was last written, and '
                  'it will still be paid. Usually the employee has lost '
                  'their department: set it and import again.',
                  scope=(self.department_id.name or self.site_id.name
                         or _('this batch’s scope')))))

        if log:
            Skip.create(log)

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
        if no_factor:
            message += _(
                "\n%(count)s had loads in BAS with NO «رد الفاتورة» on any "
                "of them and were skipped — BAS has not been writing that "
                "column since 6 Sep 2026: %(names)s",
                count=len(no_factor),
                names=self._name_list(no_factor))
        if part_factor:
            message += _(
                "\n%(count)s had some loads with no «رد الفاتورة»; those "
                "loads are not counted in the weighted quantity: %(names)s",
                count=len(part_factor),
                names=self._name_list(part_factor))
        if out_of_scope:
            message += _(
                "\n%(count)s already had a line here but are outside this "
                "batch's scope, so it could NOT be refreshed and the old "
                "figure stands: %(names)s",
                count=len(out_of_scope),
                names=self._name_list(out_of_scope))
        if cash_rerated:
            message += _(
                "\n%(count)s had cash loads re-rated from the amount bands "
                "instead of the till's own rate — see Not Imported for the "
                "figures: %(names)s",
                count=len(cash_rerated),
                names=self._name_list(cash_rerated))
        if no_attendance:
            message += _(
                "\n%(count)s have no attendance recorded for this month, so "
                "the FULL required trips were applied — check their sheet "
                "before approving: %(names)s",
                count=len(no_attendance),
                names=self._name_list(no_attendance))
        if on_vacation:
            message += _(
                "\n%(count)s were on vacation this month and were skipped — "
                "their trips were settled on the leave request: %(names)s",
                count=len(on_vacation),
                names=self._name_list(on_vacation))
        if part_month:
            message += _(
                "\n%(count)s came back from vacation during the month; only "
                "their loads from the return date onwards were counted: "
                "%(names)s",
                count=len(part_month),
                names=self._name_list(part_month))
        self.sudo().message_post(body=message, subtype_xmlid='mail.mt_note')
        return self._notify(message, title=_('Imported from BAS'))

    @staticmethod
    def _name_list(employees, limit=5):
        """First few employee names, with a count of the rest."""
        names = ', '.join(employees[:limit].mapped('name'))
        if len(employees) > limit:
            names += _(' and %(more)s more', more=len(employees) - limit)
        return names

    def _bas_required_trips(self, worked_days, period=None):
        """The free allowance: required trips pro-rated to days worked.

        ``base`` is the figure for a **full month**, from the one hidden
        settings record (Configuration → Driver Trip Settings) — which is
        what "the calculation for all locations" means. It used to come
        from the batch's work site; with the batch scoped to a department
        there is no site to read.

        **Divided by the days in that month, not by a fixed 30** (Sep
        2026). The old constant meant a driver present every day of a
        31-day month owed ``round(50 * 31 / 30)`` = **52** trips before
        earning anything — more than the full-month figure he was
        configured against — while February let him off with 47. The same
        correction payroll made to its daily-wage divisor. Capped at
        ``base`` so a full month can never ask for more than a full
        month's worth.

        ``worked_days is None`` means nothing recorded the month at all:
        the full requirement stands, and the importer names him in the
        summary rather than letting it pass unseen.
        """
        self.ensure_one()
        base = self.env['ksw.site']._trip_settings().required_trips_full_month
        base = base or 0
        if worked_days is None:
            return float(base)
        period = period or self.period
        days_in_month = calendar.monthrange(period.year, period.month)[1]
        return float(min(
            base, round(base * (worked_days or 0) / float(days_in_month))))

    def _invoice_factor_cutover(self):
        """The date from which «رد الفاتورة» replaces «الرد المضاعف».

        Returns a ``date``. A blank or unparseable parameter falls back to
        the default rather than disabling the cutover: an unreadable
        setting must not silently re-base every month ever imported.
        """
        raw = (self.env['ir.config_parameter'].sudo().get_param(
            'ksw_commissions.invoice_factor_from') or '').strip()
        try:
            return fields.Date.to_date(raw) or fields.Date.to_date(
                INVOICE_FACTOR_FROM_DEFAULT)
        except (ValueError, TypeError):
            _logger.warning(
                'KSW commissions: ksw_commissions.invoice_factor_from is '
                '%r, which is not a date — falling back to %s.',
                raw, INVOICE_FACTOR_FROM_DEFAULT)
            return fields.Date.to_date(INVOICE_FACTOR_FROM_DEFAULT)

    def _bas_cost_centres(self, employee):
        """Every BAS cost centre whose loads belong to this driver.

        Normally one. BAS occasionally books the same man under two
        spellings — an Arabic and an English rendering of the same name and
        number — and then his month arrives split in half with no sign on
        either piece that the other exists. `x_bas_driver_cost_center_alt`
        lets HR say so, rather than the figure being corrected by hand
        every month and destroyed by the next import.
        """
        raw = [employee.x_bas_driver_cost_center or '']
        raw += (employee.x_bas_driver_cost_center_alt or '').split(',')
        seen, keys = set(), []
        for value in raw:
            key = self._norm_cc(value)
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    @staticmethod
    def _bas_merge(source, keys):
        """Add up a driver's BAS rows across all his cost centres."""
        parts = [source[k] for k in keys if k in source]
        if not parts:
            return None
        merged = dict(parts[0])
        for extra in parts[1:]:
            merged['loads'] += extra['loads']
            merged['mult'] += extra['mult']
            merged['missing'] += extra.get('missing', 0)
            merged['new_basis'] = (merged.get('new_basis', 0)
                                   + extra.get('new_basis', 0))
            for k in ('cash_loads', 'cash_at_till', 'cash_at_band'):
                merged[k] = merged.get(k, 0) + extra.get(k, 0)
        return merged

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
        'missing': int, 'equip': str, 'raw_cc': str}}`` for every driver with
        at least one water-load line in the half-open range
        ``[date_from, date_to)``.  ``mult`` is Σ «رد الفاتورة» and
        ``missing`` counts the lines that carry none, so the caller can tell
        "weightless" from "not recorded".
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
            # A cash sale sits on the till, not on a customer, so neither
            # basis describes where it went — see ksw_pay_cash_band. Its
            # factor comes from the line amount instead, on both sides of
            # the cutover, because the band answers "how far", which is
            # what both columns are for.
            col = INVOICE_FACTOR_COLUMN
            Band = self.env['ksw.pay.cash.band'].sudo()
            cash_accounts = Band._cash_accounts()
            band_case = Band._sql_case('s.AMOUNT') if cash_accounts else None
            if band_case:
                cash_ph = ','.join(['%s'] * len(cash_accounts))
                is_cash = 's.FCODE IN (' + cash_ph + ')'
            else:
                # No bands configured: leave every line exactly as it was.
                cash_accounts, is_cash = (), '1 = 0'
                band_case = '0'
            factor_expr = (
                "CASE WHEN " + is_cash + " THEN " + band_case + " "
                "     WHEN h.FDATE >= %s THEN ISNULL(s." + col + ", 0) "
                "     ELSE ISNULL(c.FACTORE, 0) END")

            # Both bases, chosen per line by the invoice's own date:
            # before the cutover the customer's current multiplier
            # («الرد المضاعف», cod10.FACTORE), from it on the multiplier
            # recorded on the invoice («رد الفاتورة»). `missing` counts
            # only lines judged on the NEW basis — a pre-cutover line has
            # nothing to be missing.
            sql = (
                "SELECT h.COST_CENTER2 AS driver_cc, "
                "       COUNT(*) AS loads, "
                "       SUM(" + factor_expr + ") AS mult, "
                # 'missing' is about «رد الفاتورة» only, so a cash line can
                # never be missing — its factor came from the band.
                "       SUM(CASE WHEN NOT (" + is_cash + ") "
                "                 AND h.FDATE >= %s "
                "                 AND ISNULL(s." + col + ", 0) = 0 "
                "                THEN 1 ELSE 0 END) AS missing, "
                "       SUM(CASE WHEN h.FDATE >= %s THEN 1 ELSE 0 END) "
                "           AS new_basis, "
                # What the cash lines are, and what they would have been
                # worth on the till's own rate — so the change can be
                # reported rather than applied silently.
                "       SUM(CASE WHEN " + is_cash + " THEN 1 ELSE 0 END) "
                "           AS cash_loads, "
                "       SUM(CASE WHEN " + is_cash + " "
                "                THEN ISNULL(c.FACTORE, 0) ELSE 0 END) "
                "           AS cash_at_till, "
                "       SUM(CASE WHEN " + is_cash + " THEN " + band_case
                + " ELSE 0 END) AS cash_at_band, "
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
            cutover = fields.Date.to_string(self._invoice_factor_cutover())
            cash = list(cash_accounts)
            params = tuple(
                # order matters: every %s in the SELECT, left to right
                cash + [cutover]                 # mult
                + cash + [cutover]               # missing
                + [cutover]                      # new_basis
                + cash                           # cash_loads
                + cash                           # cash_at_till
                + cash                           # cash_at_band
                + ftypes + items
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
                missing = int(row.get('missing') or 0)
                new_basis = int(row.get('new_basis') or 0)
                cash_loads = int(row.get('cash_loads') or 0)
                cash_till = float(row.get('cash_at_till') or 0.0)
                cash_band = float(row.get('cash_at_band') or 0.0)
                if key in result:
                    # Two raw spellings collapse to the same driver — merge.
                    result[key]['loads'] += loads
                    result[key]['mult'] += mult
                    result[key]['missing'] += missing
                    result[key]['new_basis'] += new_basis
                    result[key]['cash_loads'] += cash_loads
                    result[key]['cash_at_till'] += cash_till
                    result[key]['cash_at_band'] += cash_band
                else:
                    result[key] = {
                        'loads': loads, 'mult': mult, 'missing': missing,
                        'new_basis': new_basis, 'cash_loads': cash_loads,
                        'cash_at_till': cash_till, 'cash_at_band': cash_band,
                        'equip': (row['equip'] or '').strip(),
                        'raw_cc': raw.replace('\xa0', ' ').strip(),
                    }
        finally:
            conn.close()
        return result

    def _get_worked_days_from_sheet(self, employee, period):
        """Attended days from the employee's monthly attendance sheet.

        **This is the figure the free allowance is pro-rated against**: if
        the supervisor has marked four days attended, four days is what the
        driver is asked to earn against, not a full month.

        Returns the sheet's ``total_attended`` — a plain count of lines
        flagged ``is_attended`` — for the period's month. A **confirmed**
        sheet wins over a draft one, but a draft is used rather than
        nothing: the commission month is prepared before the sheets are
        signed off, and refusing to read a draft would charge every driver
        a full month's requirement.

        ``None`` when the month was not recorded anywhere, so the caller
        can tell "he worked no days" from "nobody said".
        """
        Sheet = self.env['ksw.attendance.sheet'].sudo()
        domain = [
            ('employee_id', '=', employee.id),
            ('month', '=', str(period.month)),
            ('year', '=', period.year),
        ]
        sheet = Sheet.search(domain + [('state', '=', 'confirmed')], limit=1) \
            or Sheet.search(domain, limit=1)
        if sheet:
            return sheet.total_attended
        return self._get_worked_days_from_attendance(employee, period)

    def _get_worked_days_from_attendance(self, employee, period):
        """Days worked for a driver who punches instead of being marked.

        About one driver in ten carries a BAS cost centre but no monthly
        sheet — he is on the biometric device. Without this he fell into
        the ``None`` branch and was charged the **full** month's required
        trips whatever he actually did, which is the opposite of the rule
        the sheet-based drivers get.

        Counted as distinct calendar days with a real punch. Absence rows
        are ``hr.attendance`` records too in this database
        (``x_is_absent``), so they have to be excluded or every driver
        would come out at a full month.
        """
        Attendance = self.env['hr.attendance'].sudo()
        if 'x_is_absent' not in Attendance._fields:
            return None
        start = period.replace(day=1)
        end = start + relativedelta(months=1)
        records = Attendance.search([
            ('employee_id', '=', employee.id),
            ('check_in', '>=', fields.Datetime.to_datetime(start)),
            ('check_in', '<', fields.Datetime.to_datetime(end)),
            ('x_is_absent', '=', False),
        ])
        if not records:
            return None
        return len({r.check_in.date() for r in records if r.check_in})
