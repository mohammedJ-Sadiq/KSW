"""KSW Driver Commission Sheet & Line — per-site monthly trip tally.

Phase B model. The supervisor opens a driver-commission sheet for a site
and fills in one line per driver (employee).  The tiered commission is
computed automatically with the rates defined on ``ksw.site``, then
written back to the parent ``ksw.commission.sheet`` as the read-only
``driver_commission_amount``.
"""
import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class KswDriverCommissionSheet(models.Model):
    """One driver-commission tally per (site, period)."""
    _name = 'ksw.driver.commission.sheet'
    _description = 'KSW Driver Commission Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, site_id'

    name = fields.Char(readonly=True, default='New', copy=False)
    site_id = fields.Many2one(
        'ksw.site', required=True, ondelete='restrict', tracking=True,
    )
    period = fields.Date(
        required=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        tracking=True,
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('confirmed', 'Confirmed')],
        default='draft', required=True, copy=False, tracking=True,
    )
    is_locked = fields.Boolean(readonly=True, copy=False)
    line_ids = fields.One2many(
        'ksw.driver.commission.line', 'sheet_id', copy=True,
    )
    currency_id = fields.Many2one(
        'res.currency', default=lambda s: s.env.company.currency_id,
        required=True,
    )
    total_commission = fields.Monetary(
        compute='_compute_total', store=True,
    )

    _unique_site_period = models.Constraint(
        'UNIQUE(site_id, period)',
        'Only one driver commission sheet per site per month.',
    )

    @api.depends('line_ids.total_commission')
    def _compute_total(self):
        for rec in self:
            rec.total_commission = sum(rec.line_ids.mapped('total_commission'))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence']
        for v in vals_list:
            if not v.get('name') or v['name'] == 'New':
                v['name'] = seq.next_by_code('ksw.driver.commission.sheet') or 'New'
            if v.get('period'):
                d = fields.Date.to_date(v['period'])
                v['period'] = d.replace(day=1)
        return super().create(vals_list)

    def action_confirm(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft sheets can be confirmed."))
            rec.write({'state': 'confirmed', 'is_locked': True})
            # Push commission amounts to the linked commission sheets.
            rec._sync_to_commission_sheets()
            rec.message_post(
                body=_('Driver commission sheet confirmed. Total: %(t).2f',
                       t=rec.total_commission),
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_to_draft(self):
        for rec in self:
            rec.write({'state': 'draft', 'is_locked': False})
            rec._sync_to_commission_sheets()

    def _sync_to_commission_sheets(self):
        """Recompute driver_commission_amount on linked commission sheets.

        Uses sudo() so that already-Done commission sheets receive the
        updated driver commission without triggering the write guard.
        Auto-creates a draft commission sheet for any newly added driver
        that does not yet have one for this period (template lines are
        applied automatically via the sheet's create() hook).
        """
        Sheet = self.env['ksw.commission.sheet']
        for rec in self:
            for line in rec.line_ids:
                if not line.employee_id:
                    continue
                sheet = Sheet.sudo().search([
                    ('employee_id', '=', line.employee_id.id),
                    ('period', '=', rec.period),
                ], limit=1)
                if not sheet:
                    # New driver added — create their commission sheet so
                    # the driver commission amount is visible immediately.
                    sheet = Sheet.sudo().create({
                        'employee_id': line.employee_id.id,
                        'period': rec.period,
                    })
                # Recompute in sudo context so the write guard on Done
                # commission sheets is bypassed (this is a system-level
                # sync, not a human edit).
                sheet.sudo()._compute_driver_commission()
                sheet.sudo().flush_recordset(['driver_commission_amount'])

    # ==================================================================
    # Pull driver trips from BAS (bas9ss)
    # ------------------------------------------------------------------
    # The BAS report «الحركة التجارية للأصناف» (commercial item movement),
    # filtered by warehouse + period, is the source of truth for driver
    # trips. Each water-tanker delivery line (item 11032 «مياه عذبة تريلات»,
    # ~32 m³ = one load / «ردّة») carries the driver on ``COST_CENTER2`` and
    # the equipment (T-code) on ``COST_CENTER``.  The two report measures are:
    #   • عدد الردود   (actual_trips)     = COUNT of load lines for the driver
    #   • الرد المضاعف (multiplied_trips) = Σ cod10.FACTORE of each line's
    #                                       customer (per-destination distance
    #                                       factor; join STR10.FCODE=cod10.DCODE1)
    # Drivers are matched to Odoo employees via
    # ``hr.employee.x_bas_driver_cost_center`` (the BAS label differs from the
    # Odoo name). Configurable via ir.config_parameter:
    #   ksw_commissions.orood_item_codes  (default '11032')
    #   ksw_commissions.orood_ftypes      (default '600')
    # ==================================================================
    def action_pull_from_bas(self):
        """Fill each site driver's trips from BAS for this sheet's period."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Pull from BAS is only available while the sheet is in Draft."))
        if not self.env.su and not self.env.user.has_group(
                'KSW_commissions.group_commission_supervisor'):
            raise UserError(_(
                "Only commission supervisors can pull driver trips from BAS."))
        if not self.site_id:
            raise UserError(_("Select a Site before pulling from BAS."))

        period = self.period
        date_from = period.replace(day=1)
        date_to = date_from + relativedelta(months=1)

        # Roster = all attendance-sheet employees assigned to this site.
        # Every roster member gets a row (honouring "collect all site drivers
        # automatically"); mapped drivers are filled from BAS, unmapped ones
        # get zero trips and are flagged for the supervisor.
        employees = self.env['hr.employee'].search([
            ('x_site_id', '=', self.site_id.id),
            ('x_is_attendance_sheet', '=', True),
        ])
        # BAS trips keyed by NORMALISED driver cost center (BAS stores the
        # «مركز تكلفة الموظف» with non-breaking spaces / mixed case, so exact
        # matching is fragile — see _norm_cc).
        bas_data = self._bas_fetch_orood(date_from, date_to)

        existing = {l.employee_id.id: l for l in self.line_ids}
        commands = []
        filled = 0
        no_data = self.env['hr.employee']
        missing_cc = self.env['hr.employee']
        for emp in employees:
            cc = (emp.x_bas_driver_cost_center or '').strip()
            data = bas_data.get(self._norm_cc(cc)) if cc else None
            vals = {
                'actual_trips': data['loads'] if data else 0,
                'multiplied_trips': data['mult'] if data else 0.0,
            }
            worked = self._get_worked_days_from_sheet(emp, period)
            if worked is not None:
                vals['worked_days'] = worked
            if emp.id in existing:
                commands.append((1, existing[emp.id].id, vals))
            else:
                vals['employee_id'] = emp.id
                commands.append((0, 0, vals))
            if data:
                filled += 1
            elif not cc:
                missing_cc |= emp
            else:
                no_data |= emp

        if commands:
            self.write({'line_ids': commands})

        return self._pull_from_bas_feedback(filled, no_data, missing_cc)

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

    def _pull_from_bas_feedback(self, filled, no_data, missing_cc):
        """Post a chatter audit note and return a UI notification summary."""
        period_label = self.period.strftime('%B %Y')
        body = _(
            "Pull from BAS — %(site)s, %(period)s: filled %(n)s driver(s).",
            site=self.site_id.name, period=period_label, n=filled)
        if no_data:
            body += _(
                " No BAS loads found for: %s.") % ', '.join(no_data.mapped('name'))
        if missing_cc:
            body += _(
                " Missing BAS cost center (set it on the employee): %s."
            ) % ', '.join(missing_cc.mapped('name'))
        self.message_post(body=body, subtype_xmlid='mail.mt_note')

        has_issue = bool(no_data or missing_cc)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Pull from BAS"),
                'message': body,
                'type': 'warning' if has_issue else 'success',
                'sticky': has_issue,
            },
        }

    # ==================================================================
    # Report helpers — called from QWeb templates.
    # Lambdas that close over outer-scope variables are forbidden in
    # QWeb's safe_eval, so all aggregation lives here.
    # ==================================================================
    def _report_get_period_labels(self):
        """Return 'Month YYYY' labels for all distinct periods, newest first."""
        periods = sorted(
            {o.period for o in self if o.period}, reverse=True)
        return [p.strftime('%B %Y') for p in periods]

    def _report_get_driver_rollup(self):
        """Return a list of dicts for the per-driver cumulative table.

        Each dict: name, sheet_count, total_trips, total_commission.
        Sorted alphabetically by driver name.
        """
        driver_map = {}
        for ln in self.mapped('line_ids'):
            if not ln.employee_id:
                continue
            eid = ln.employee_id.id
            if eid not in driver_map:
                driver_map[eid] = {
                    'name': ln.employee_id.name or '',
                    'sheet_count': 0,
                    'total_trips': 0,
                    'total_commission': 0.0,
                }
            driver_map[eid]['sheet_count'] += 1
            driver_map[eid]['total_trips'] += ln.multiplied_trips or 0
            driver_map[eid]['total_commission'] += ln.total_commission or 0.0
        return sorted(driver_map.values(), key=lambda d: d['name'])


class KswDriverCommissionLine(models.Model):
    """One row per driver on a driver-commission sheet."""
    _name = 'ksw.driver.commission.line'
    _description = 'KSW Driver Commission Line'
    _order = 'sheet_id, sequence'

    sheet_id = fields.Many2one(
        'ksw.driver.commission.sheet', required=True, ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict',
        domain="[('x_is_attendance_sheet', '=', True)]",
    )
    vehicle_number = fields.Char()
    worked_days = fields.Integer(
        default=0, help='Actual days worked this month (entered by supervisor).',
    )

    # Trip counts
    required_trips = fields.Integer(
        compute='_compute_required_trips', store=True,
        help='Tier-1 threshold: round(site.required_trips_full_month × '
             'worked_days / 30). No commission earned until this is exceeded.',
    )
    actual_trips = fields.Integer(
        default=0,
        help='عدد الردود — count of tanker-load delivery lines. Filled from '
             'BAS by "Pull from BAS", or entered manually.',
    )
    multiplied_trips = fields.Float(
        default=0.0, digits=(16, 2),
        help='الرد المضاعف — loads weighted by each destination customer\'s '
             'BAS distance factor (cod10.FACTORE). Fractional. Filled from '
             'BAS by "Pull from BAS", or entered manually. This is the '
             'figure the tiered commission is calculated from.',
    )

    # Tier breakdown (informational)
    tier1_trips = fields.Float(compute='_compute_tiers', store=True, digits=(16, 2))
    tier2_trips = fields.Float(compute='_compute_tiers', store=True, digits=(16, 2))
    tier3_trips = fields.Float(compute='_compute_tiers', store=True, digits=(16, 2))
    tier4_trips = fields.Float(compute='_compute_tiers', store=True, digits=(16, 2))
    tier5_trips = fields.Float(compute='_compute_tiers', store=True, digits=(16, 2))

    total_commission = fields.Monetary(
        compute='_compute_tiers', store=True,
    )
    currency_id = fields.Many2one(
        related='sheet_id.currency_id', store=True, readonly=True,
    )

    # --- CRUD — auto-sync on confirmed sheets ----------------------------

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        # Sync any lines added to an already-confirmed sheet immediately.
        confirmed_sheets = lines.mapped('sheet_id').filtered(
            lambda s: s.state == 'confirmed')
        confirmed_sheets._sync_to_commission_sheets()
        return lines

    def write(self, vals):
        res = super().write(vals)
        # Re-sync if the trip data changed on a confirmed sheet.
        trip_fields = {'actual_trips', 'multiplied_trips', 'worked_days',
                       'employee_id'}
        if trip_fields & set(vals):
            confirmed_sheets = self.mapped('sheet_id').filtered(
                lambda s: s.state == 'confirmed')
            confirmed_sheets._sync_to_commission_sheets()
        return res

    # --- Computed helpers -------------------------------------------

    @api.depends('sheet_id.site_id.required_trips_full_month', 'worked_days')
    def _compute_required_trips(self):
        for l in self:
            site = l.sheet_id.site_id
            base = site.required_trips_full_month if site else 50
            l.required_trips = round(base * (l.worked_days or 0) / 30)

    @api.depends(
        'multiplied_trips', 'required_trips',
        'sheet_id.site_id.tier2_trips', 'sheet_id.site_id.tier2_rate',
        'sheet_id.site_id.tier3_trips', 'sheet_id.site_id.tier3_rate',
        'sheet_id.site_id.tier4_trips', 'sheet_id.site_id.tier4_rate',
        'sheet_id.site_id.tier5_rate',
    )
    def _compute_tiers(self):
        for l in self:
            site = l.sheet_id.site_id
            if not site:
                l.tier1_trips = l.tier2_trips = l.tier3_trips = 0
                l.tier4_trips = l.tier5_trips = 0
                l.total_commission = 0.0
                continue

            above = max((l.multiplied_trips or 0) - (l.required_trips or 0), 0)
            l.tier1_trips = (l.required_trips or 0)

            # Waterfall through tiers 2–5
            t2 = min(above, site.tier2_trips)
            above -= t2
            t3 = min(above, site.tier3_trips)
            above -= t3
            t4 = min(above, site.tier4_trips)
            above -= t4
            t5 = above  # remainder

            l.tier2_trips = t2
            l.tier3_trips = t3
            l.tier4_trips = t4
            l.tier5_trips = t5

            l.total_commission = (
                t2 * (site.tier2_rate or 0.0)
                + t3 * (site.tier3_rate or 0.0)
                + t4 * (site.tier4_rate or 0.0)
                + t5 * (site.tier5_rate or 0.0)
            )
