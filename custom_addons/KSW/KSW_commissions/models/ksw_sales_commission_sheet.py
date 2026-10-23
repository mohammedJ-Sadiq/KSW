"""KSW Sales / Collection Commission Sheet — monthly accountant entry.

Single source of truth for sales-commission and collection-commission
amounts. Each month the accountant opens (or auto-creates) one sheet
and adds one row per salesperson with the achieved sales /
collection amounts pulled from their external accountant module.

**Not part of the commission request.** Until 19.0.2.0.0 confirming this
sheet pushed its amounts onto the employee's ``ksw.commission.sheet``, so
sales commission was paid on the general commission bank file. It is now
managed and paid separately: nothing here reaches the commission sheet, and
this is not an entry type on the Monthly Commission Run.
"""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ksw_salesperson_profile import ROLE_SELECTION


class KswSalesCommissionSheet(models.Model):
    """Sales / collection commission — deliberately NOT a commission entry type.

    This used to push its amounts onto the employee's monthly
    ``ksw.commission.sheet``, so it was paid on the general commission bank
    file. As of 19.0.2.0.0 sales and collection are **not part of the
    commission request**: the sheet stands alone, it contributes nothing, and
    it is not a card on the Monthly Commission Run.

    Concretely that means this model does *not* inherit
    ``ksw.commission.source.mixin`` — inheriting it IS the registration — and
    keeps its own sequence / state / confirm-reset implementation below.

    Historical amounts are untouched: the three deprecated
    ``*_commission_amount`` shims on ``ksw.commission.sheet`` are still
    backfilled by migration 19.0.2.0.0, so sheets that already included a
    sales commission keep the total they were paid on. Only new
    contributions stop.
    """
    _name = 'ksw.sales.commission.sheet'
    _description = 'KSW Sales / Collection Commission Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period desc, id desc'

    name = fields.Char(readonly=True, default='New', copy=False)
    period = fields.Date(
        required=True, tracking=True,
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        help='First day of the month covered by this sheet.',
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('confirmed', 'Confirmed')],
        default='draft', required=True, copy=False, tracking=True,
    )
    is_locked = fields.Boolean(readonly=True, copy=False)
    x_bas_detail_ids = fields.One2many(
        'ksw.sales.commission.bas.line', 'sheet_id', string='BAS Detail')
    x_bas_detail_count = fields.Integer(compute='_compute_bas_detail_count')
    x_unmapped_rep_count = fields.Integer(
        'BAS Reps to Link', compute='_compute_unmapped_rep_count',
        help='BAS rep codes with customers but no employee linked: their '
             'sales and collections are credited to nobody.')
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda s: s.env.company.currency_id,
    )

    line_ids = fields.One2many(
        'ksw.sales.commission.line', 'sheet_id', copy=True,
    )
    total_commission = fields.Monetary(
        compute='_compute_total', store=True,
    )
    total_sales_commission = fields.Monetary(
        compute='_compute_total', store=True,
    )
    total_collection_commission = fields.Monetary(
        compute='_compute_total', store=True,
    )
    total_combined_commission = fields.Monetary(
        compute='_compute_total', store=True,
    )

    _unique_period = models.Constraint(
        'UNIQUE(period)',
        'Only one sales/collection commission sheet per month.',
    )

    @api.depends('line_ids.total_commission',
                 'line_ids.sales_commission_amount',
                 'line_ids.collection_commission_amount',
                 'line_ids.combined_commission_amount')
    def _compute_total(self):
        for rec in self:
            rec.total_sales_commission = sum(
                rec.line_ids.mapped('sales_commission_amount'))
            rec.total_collection_commission = sum(
                rec.line_ids.mapped('collection_commission_amount'))
            rec.total_combined_commission = sum(
                rec.line_ids.mapped('combined_commission_amount'))
            rec.total_commission = sum(
                rec.line_ids.mapped('total_commission'))

    # ------------------------------------------------------------------
    # CRUD + state (standalone — this model is not an entry type)
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        Seq = self.env['ir.sequence']
        for v in vals_list:
            if not v.get('name') or v['name'] == 'New':
                v['name'] = (
                    Seq.next_by_code('ksw.sales.commission.sheet') or 'New')
            if v.get('period'):
                v['period'] = fields.Date.to_date(
                    v['period']).replace(day=1)
        return super().create(vals_list)

    def _check_sales_sheet_group(self):
        if self.env.su or self.env.user.has_group(
                'KSW_commissions.group_entry_sales'):
            return
        raise UserError(_(
            "You are not allowed to confirm or reset a sales/collection "
            "commission sheet."))

    def action_confirm(self):
        self._check_sales_sheet_group()
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft sheets can be confirmed."))
            rec.write({'state': 'confirmed', 'is_locked': True})
            rec.message_post(
                body=_('Sales/collection commission sheet confirmed. '
                       'Total: %(t).2f', t=rec.total_commission),
                subtype_xmlid='mail.mt_note',
            )

    def action_reset_to_draft(self):
        self._check_sales_sheet_group()
        for rec in self:
            rec.write({'state': 'draft', 'is_locked': False})

    def action_open_import_wizard(self):
        """Open the Excel import wizard pre-filled with this sheet."""
        self.ensure_one()
        return {
            'name': _("Import Sales & Collection from Excel"),
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.sales.commission.import.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_sheet_id': self.id,
            },
        }

    def _compute_bas_detail_count(self):
        for rec in self:
            rec.x_bas_detail_count = len(rec.x_bas_detail_ids)

    def _compute_unmapped_rep_count(self):
        count = self.env['ksw.bas.sales.rep'].sudo().search_count(
            [('state', '=', 'todo')])
        for rec in self:
            rec.x_unmapped_rep_count = count

    def action_open_bas_sales_reps(self):
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_commissions.action_ksw_bas_sales_rep')
        action['context'] = {'search_default_todo': 1}
        return action

    # ------------------------------------------------------------------
    # Pull achieved sales/collection directly from BAS
    # ------------------------------------------------------------------
    def action_pull_from_bas(self):
        """Read this month's sales, collections and collection targets from
        the BAS customer ledger and write them onto the sheet's lines.

        How each figure is built (which documents, VAT, which ledgers, who
        gets credited, how the target is worked out) is configuration —
        see ``ksw.sales.commission.bas`` and Settings > KSW Commissions >
        Sales & Collection from BAS. Sales targets still come from the
        salesperson profiles; BAS has none.

        Every customer the figures came from is written to the sheet's
        BAS Detail tab, credited or not, so the month reconciles.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                'The commission sheet must be in draft state to pull '
                'from BAS.'))
        if not self.period:
            raise UserError(_('Set the period before pulling from BAS.'))

        # New rep codes and renamed labels first, so attribution never runs
        # on a stale mapping.
        Rep = self.env['ksw.bas.sales.rep']
        Rep._sync_from_bas_customers()
        self.env['ksw.bas.customer'].sudo()._recompute_effective_reps()
        house = Rep._house_codes()

        figures = self.env['ksw.sales.commission.bas']._period_figures(
            self.period)
        date_from, date_to = figures['start'], figures['end']

        # One bucket per (employee, commission type): the workbook pays a
        # person separately as Salesman, Collector and Salesman & Collector,
        # each on its own customers.
        buckets = {}
        gaps = {'house': {}, 'unmapped': {}, 'role': {}}
        detail = []

        def _bucket(row):
            return buckets.setdefault(
                (row['employee'], row['ctype']),
                {'sales': 0.0, 'by_account': {}, 'collected': 0.0,
                 'target': 0.0})

        def _status(row):
            if row['employee']:
                return 'credited'
            if row.get('blocked'):
                return 'role'
            return 'house' if row['code'] in house else 'unmapped'

        def _gap(row, idx):
            name = row['rep_name'] or (
                row['customer'].seller_name if idx == 0
                else row['customer'].collector_name) or ''
            if row.get('blocked'):
                bucket = gaps['role'].setdefault(
                    (row['code'], name), [0.0, 0.0, set()])
                bucket[2].add(row['account'])
            elif row['code'] in house:
                bucket = gaps['house'].setdefault(name or row['code'],
                                                  [0.0, 0.0])
            else:
                bucket = gaps['unmapped'].setdefault(
                    (row['code'], name), [0.0, 0.0, set()])
                bucket[2].add(row['account'])
            bucket[idx] += row['net']

        for row in figures['sales']:
            if row['employee']:
                b = _bucket(row)
                b['sales'] += row['net']
                acc = row['account'].lower()
                b['by_account'][acc] = b['by_account'].get(acc, 0.0) + row['net']
            else:
                _gap(row, 0)
            detail.append(('sales', row))

        grand_collected = 0.0
        for row in figures['collections']:
            grand_collected += row['net']
            if row['employee']:
                _bucket(row)['collected'] += row['net']
            else:
                _gap(row, 1)
            detail.append(('collection', row))

        grand_target = 0.0
        if figures['cfg']['target_method'] == 'due_balance':
            for row in figures['targets']:
                # A No Commission code (defaulted accounts) stays out of the
                # company-wide target, as in the workbook's "without
                # defaulted" total.
                if row['code'] not in house:
                    grand_target += row['net']
                if row['employee']:
                    _bucket(row)['target'] += row['net']
                detail.append(('target', row))
        else:
            grand_target = None

        excluded = self._apply_sales_exclusions(buckets)
        imported_lines = self._apply_bas_typed(
            buckets, grand_collected, grand_target)
        self._write_bas_detail(detail, _status, excluded)
        self._post_bas_pull_summary(
            date_from, date_to, imported_lines, gaps, figures['cfg'])

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.sales.commission.sheet',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _apply_sales_exclusions(self, buckets):
        """Take out of each employee's sales the customers his profile
        lists under Sales Left Out. Returns ``{(employee_id, account)}``."""
        excluded = set()
        year = fields.Date.to_date(self.period).year
        Profile = self.env['ksw.salesperson.profile'].sudo()
        for (employee, _ctype), bucket in buckets.items():
            profile = Profile.search([
                ('employee_id', '=', employee.id), ('year', '=', year),
                ('active', '=', True)], limit=1)
            accounts = {a.strip().lower() for a in (
                profile.x_sales_excluded_accounts or '').split(',')
                if a.strip()}
            for acc in accounts & set(bucket['by_account']):
                bucket['sales'] -= bucket['by_account'].pop(acc)
                excluded.add((employee.id, acc))
        return excluded

    def _apply_bas_typed(self, buckets, grand_collected, grand_target):
        """Write one line per (employee, commission type) from the pull.

        The month is rebuilt from BAS: every line's BAS figures are cleared
        first, manual adjustments are kept and re-applied, an employee's
        pre-type line (from before 19.0.4.51.0) is taken over by his first
        type, and a leftover pre-type line with nothing manual on it is
        removed. Split lines and the Collection-Based-on-Total line work as
        before.
        """
        self.ensure_one()
        Line = self.env['ksw.sales.commission.line']
        Profile = self.env['ksw.salesperson.profile'].sudo()
        year = fields.Date.to_date(self.period).year
        imported = []

        for line in self.line_ids:
            line.write({
                'achieved_sales': 0.0 if line.split_id
                else max(0.0, line.x_sales_adjustment),
                'achieved_collection': 0.0})

        general = self.line_ids.filtered(lambda l: not l.split_id)
        untyped = general.filtered(lambda l: not l.x_commission_type)
        order = {'both': 0, 'sales': 1, 'collect': 2}
        for (employee, ctype), b in sorted(
                buckets.items(),
                key=lambda kv: (kv[0][0].name or '', order[kv[0][1]])):
            line = general.filtered(
                lambda l: l.employee_id == employee
                and l.x_commission_type == ctype)[:1]
            status = 'updated'
            if not line:
                line = untyped.filtered(
                    lambda l: l.employee_id == employee)[:1]
                if line:
                    untyped -= line
                    line.x_commission_type = ctype
                else:
                    line = Line.create({
                        'sheet_id': self.id, 'employee_id': employee.id,
                        'x_commission_type': ctype})
                    general |= line
                    status = 'created'
            line.write({
                'achieved_sales': max(0.0, b['sales'] + line.x_sales_adjustment),
                'achieved_collection': b['collected'],
            })
            imported.append((
                f"{employee.display_name} — "
                f"{dict(ROLE_SELECTION)[ctype]}",
                b['sales'], b['collected'], b['target'], status, [], []))

        # Pre-type lines nobody took over: gone unless something manual
        # sits on them.
        for line in untyped:
            if not (line.x_addition_line_ids or line.x_deduction_line_ids
                    or line.x_condition_override or line.x_sales_adjustment
                    or line.x_target_collection_adjustment):
                line.unlink()

        # Client splits: their clients' sales, across every type.
        by_emp = {}
        for (employee, _ctype), b in buckets.items():
            acc_map = by_emp.setdefault(employee, {})
            for acc, amt in b['by_account'].items():
                acc_map[acc] = acc_map.get(acc, 0.0) + amt
        for employee, acc_map in by_emp.items():
            profile = Profile.search([
                ('employee_id', '=', employee.id), ('year', '=', year),
                ('active', '=', True)], limit=1)
            for split in profile.split_ids:
                accounts = {
                    (p.x_client_account_number or '').strip().lower()
                    for p in split.rule_id.partner_ids
                    if p.x_client_account_number}
                split_sales = sum(a for k, a in acc_map.items()
                                  if k in accounts)
                line = self.line_ids.filtered(
                    lambda l: l.employee_id == employee
                    and l.split_id == split)[:1]
                if not line:
                    line = Line.create({
                        'sheet_id': self.id, 'employee_id': employee.id,
                        'split_id': split.id})
                line.achieved_sales = split_sales
                imported.append((
                    f"{employee.display_name} [{split.label}]",
                    split_sales, None, None, 'updated (split)', [], []))

        # Collection Based on Total: the company's whole month.
        for profile in Profile.search([
                ('year', '=', year), ('active', '=', True),
                ('x_collection_based_on_total', '=', True)]):
            employee = profile.employee_id
            line = self.line_ids.filtered(
                lambda l: l.employee_id == employee and not l.split_id
                and l.x_commission_type == profile.role)[:1]
            if not line:
                line = Line.create({
                    'sheet_id': self.id, 'employee_id': employee.id,
                    'x_commission_type': profile.role})
            line.achieved_collection = grand_collected
            imported.append((
                f"{employee.display_name} [Total Collection]",
                None, grand_collected, grand_target, 'updated', [], []))

        # Role and both targets follow the profile and the BAS figures as
        # they are now.
        self.line_ids._compute_role_and_targets()
        for profile in Profile.search([
                ('year', '=', year), ('active', '=', True),
                ('x_collection_based_on_total', '=', True)]):
            line = self.line_ids.filtered(
                lambda l: l.employee_id == profile.employee_id
                and not l.split_id
                and l.x_commission_type == profile.role)[:1]
            if line and grand_target is not None:
                line.target_collection = max(
                    0.0, grand_target + line.x_target_collection_adjustment)
        return imported

    def _write_bas_detail(self, detail, status, excluded=()):
        """Replace this sheet's BAS Detail rows with what was just read."""
        self.ensure_one()
        Detail = self.env['ksw.sales.commission.bas.line'].sudo()
        Detail.search([('sheet_id', '=', self.id)]).unlink()
        vals = []
        for kind, row in detail:
            customer = row['customer']
            vals.append({
                'sheet_id': self.id,
                'kind': kind,
                'employee_id': row['employee'].id or False,
                'status': 'excluded' if kind == 'sales' and (
                    row['employee'].id, row['account'].lower()) in excluded
                    else status(row),
                'rep_code': row['code'],
                'commission_type': row.get('ctype'),
                'rep_name': row['rep_name'] or (
                    customer.collector_name if kind != 'sales'
                    else customer.seller_name) if customer else row['rep_name'],
                'account': row['account'],
                'customer_name': (customer.name_ar or customer.name_en)
                if customer else '',
                'term_days': row.get('term_days', 0),
                'balance': row.get('balance', 0.0),
                'not_due': row.get('not_due', 0.0),
                'gross': row['gross'],
                'net': row['net'],
                'doc_count': row.get('count', 0),
            })
        Detail.create(vals)

    def action_open_bas_detail(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'KSW_commissions.action_ksw_sales_commission_bas_line')
        action['domain'] = [('sheet_id', '=', self.id)]
        action['context'] = {'search_default_group_employee': 1,
                             'search_default_group_type': 1,
                             'search_default_group_kind': 1}
        action['name'] = _('BAS Detail: %s', self.name)
        return action

    # ------------------------------------------------------------------
    # Shared upsert logic — used by both the Excel import wizard and
    # action_pull_from_bas().
    # ------------------------------------------------------------------
    def _apply_commission_data(self, sales_by_employee, collection_by_employee,
                                collection_grand_totals=None):
        """Upsert commission lines from aggregated sales/collection data.

        ``sales_by_employee``: ``{hr.employee: {'total': float,
        'by_account': {account_lower: amt}, 'by_customer': {name_lower: amt}}}``

        ``collection_by_employee``: ``{hr.employee: {'collected': float,
        'target': float or None}}`` — ``target`` is ``None`` when the data
        source (e.g. BAS) has no target concept; the existing
        ``target_collection`` value (computed from the salesperson
        profile) is then left untouched.

        ``collection_grand_totals``: optional ``(collected, target)`` tuple
        used for the "Collection Based on Total" manager pass — the target
        may be ``None`` for the same reason as above.

        Returns the ``imported_lines`` list consumed by
        :meth:`_format_commission_lines_html` for the caller's chatter
        summary.
        """
        self.ensure_one()
        Line = self.env['ksw.sales.commission.line']
        Profile = self.env['ksw.salesperson.profile']
        imported_lines = []
        sheet_year = (
            fields.Date.to_date(self.period).year if self.period else None
        )
        all_employees = set(sales_by_employee) | set(collection_by_employee)

        for employee in sorted(all_employees, key=lambda e: e.name or ''):
            emp_sales = sales_by_employee.get(employee)
            coll_data = collection_by_employee.get(employee)
            coll_amt = coll_data['collected'] if coll_data else None
            target_amt = coll_data.get('target') if coll_data else None

            # ----------------------------------------------------------
            # Fetch profile splits for this employee / year
            # ----------------------------------------------------------
            splits = self.env['ksw.salesperson.profile.client.split']
            if sheet_year:
                profile = Profile.sudo().search([
                    ('employee_id', '=', employee.id),
                    ('year', '=', sheet_year),
                    ('active', '=', True),
                ], limit=1)
                if profile:
                    splits = profile.split_ids

            # ----------------------------------------------------------
            # Handle split lines (each covers a named client bucket)
            # ----------------------------------------------------------
            for split in splits:
                acc_keys = {}    # acc_lower → partner display label
                name_keys = {}   # name_lower → partner display label
                for p in split.rule_id.partner_ids:
                    acc = (p.x_client_account_number or '').strip().lower()
                    alias = (p.x_commission_import_name or '').strip().lower()
                    pname = (p.name or '').strip().lower()
                    if p.x_client_account_number:
                        label = f"{p.x_client_account_number} — {p.name or ''}"
                    else:
                        label = p.name or '?'
                    if acc:
                        acc_keys[acc] = label
                    elif alias:
                        name_keys[alias] = label
                    elif pname:
                        name_keys[pname] = label

                split_sales = 0.0
                matched_detail = []
                unmatched_partners = [
                    p.x_client_account_number or p.name
                    for p in split.rule_id.partner_ids
                ]
                if emp_sales:
                    for acc_lower, amt in emp_sales.get('by_account', {}).items():
                        if acc_lower in acc_keys:
                            split_sales += amt
                            lbl = acc_keys[acc_lower]
                            matched_detail.append((lbl, amt))
                            if lbl in unmatched_partners:
                                unmatched_partners.remove(lbl)
                    for cust_lower, amt in emp_sales.get('by_customer', {}).items():
                        if cust_lower in name_keys:
                            split_sales += amt
                            lbl = name_keys[cust_lower]
                            matched_detail.append((lbl, amt))
                            if lbl in unmatched_partners:
                                unmatched_partners.remove(lbl)

                existing_split = Line.search([
                    ('sheet_id', '=', self.id),
                    ('employee_id', '=', employee.id),
                    ('split_id', '=', split.id),
                ], limit=1)
                split_vals = {}
                if emp_sales is not None:
                    split_vals['achieved_sales'] = split_sales

                if existing_split:
                    if split_vals:
                        existing_split.write(split_vals)
                    status = 'updated (split)'
                else:
                    new_line = Line.create({
                        'sheet_id': self.id,
                        'employee_id': employee.id,
                        'split_id': split.id,
                    })
                    if split_vals:
                        new_line.write(split_vals)
                    status = 'created (split)'
                imported_lines.append((
                    f"{employee.display_name} [{split.label}]",
                    split_sales if emp_sales is not None else None,
                    None, None, status,
                    matched_detail, unmatched_partners,
                ))

            # ----------------------------------------------------------
            # General line — receives the FULL total sales (not reduced)
            # and all collection data.  The split lines calculate extra
            # commission on their client subset independently.
            # ----------------------------------------------------------
            general_sales = None
            if emp_sales is not None:
                general_sales = emp_sales['total']

            existing_general = Line.search([
                ('sheet_id', '=', self.id),
                ('employee_id', '=', employee.id),
                ('split_id', '=', False),
            ], limit=1)

            gen_vals = {}
            if general_sales is not None:
                gen_vals['achieved_sales'] = max(0.0, general_sales + (
                    existing_general.x_sales_adjustment
                    if existing_general else 0.0))
            if coll_amt is not None:
                gen_vals['achieved_collection'] = coll_amt
            if target_amt is not None:
                gen_vals['target_collection'] = max(0.0, target_amt + (
                    existing_general.x_target_collection_adjustment
                    if existing_general else 0.0))

            if gen_vals:
                if existing_general:
                    existing_general.write(gen_vals)
                    imported_lines.append((
                        employee.display_name, general_sales,
                        coll_amt, target_amt, 'updated', [], []))
                else:
                    new_line = Line.create({
                        'sheet_id': self.id,
                        'employee_id': employee.id,
                    })
                    new_line.write(gen_vals)
                    imported_lines.append((
                        employee.display_name, general_sales,
                        coll_amt, target_amt, 'created', [], []))

        # -- Collection Manager: total-collection pass --------------------
        # Employees whose profile has x_collection_based_on_total=True
        # receive the grand total of ALL collections (and targets, when
        # known) for the period, regardless of which rep each collection
        # was attributed to.
        if collection_grand_totals and sheet_year:
            grand_collected, grand_target = collection_grand_totals
            mgr_profiles = Profile.sudo().search([
                ('year', '=', sheet_year),
                ('active', '=', True),
                ('x_collection_based_on_total', '=', True),
            ])
            for profile in mgr_profiles:
                employee = profile.employee_id
                existing = Line.search([
                    ('sheet_id', '=', self.id),
                    ('employee_id', '=', employee.id),
                    ('split_id', '=', False),
                ], limit=1)
                mgr_vals = {'achieved_collection': grand_collected}
                if grand_target is not None:
                    mgr_vals['target_collection'] = grand_target
                if existing:
                    existing.write(mgr_vals)
                    action_lbl = 'updated'
                else:
                    new_line = Line.create({
                        'sheet_id': self.id,
                        'employee_id': employee.id,
                    })
                    new_line.write(mgr_vals)
                    action_lbl = 'created'
                imported_lines.append((
                    f"{employee.display_name} [Total Collection]",
                    None, grand_collected, grand_target,
                    action_lbl, [], [],
                ))

        return imported_lines

    # ------------------------------------------------------------------
    # Chatter summaries
    # ------------------------------------------------------------------
    def _format_commission_lines_html(self, imported_lines):
        """Shared <ul> renderer for import/pull chatter summaries."""
        self.ensure_one()
        lines_html = Markup('')
        for entry in imported_lines:
            emp_name, sales, coll, target, action = entry[:5]
            matched_detail = entry[5] if len(entry) > 5 else []
            unmatched_partners = entry[6] if len(entry) > 6 else []

            s_str = f'SAR {sales:,.2f}' if sales is not None else '—'
            c_str = f'SAR {coll:,.2f}' if coll is not None else '—'
            t_str = f'SAR {target:,.2f}' if target is not None else '—'

            # Per-client breakdown for split lines (skip zero-amount rows)
            detail_html = Markup('')
            active_rows = [(lbl, amt) for lbl, amt in matched_detail if amt]
            if active_rows:
                rows = Markup('').join(
                    Markup(
                        '<tr>'
                        '<td style="padding:2px 10px;">{lbl}</td>'
                        '<td style="padding:2px 10px;text-align:right;'
                        'font-family:monospace;">SAR {amt}</td>'
                        '</tr>'
                    ).format(lbl=lbl, amt=f'{amt:,.2f}')
                    for lbl, amt in sorted(active_rows, key=lambda x: -x[1])
                )
                detail_html += Markup(
                    '<table style="margin:4px 0 4px 16px;font-size:0.9em;'
                    'border-collapse:collapse;">'
                    '<thead><tr style="border-bottom:1px solid #ccc;">'
                    '<th style="text-align:left;padding:2px 10px;">'
                    'Client</th>'
                    '<th style="text-align:right;padding:2px 10px;">'
                    'Amount</th>'
                    '</tr></thead>'
                    '<tbody>{rows}</tbody>'
                    '</table>'
                ).format(rows=rows)
            if unmatched_partners:
                missing = Markup(', ').join(
                    escape(str(x)) for x in unmatched_partners
                )
                detail_html += Markup(
                    '<div style="margin-left:16px;font-size:0.85em;'
                    'color:#c0392b;">⚠ Not found in source: {m}</div>'
                ).format(m=missing)

            lines_html += Markup(
                '<li><b>{name}</b> [{action}] '
                'Sales: {s} | Target Coll: {t} | Collected: {c}'
                '{detail}</li>'
            ).format(
                name=escape(emp_name), action=escape(action),
                s=s_str, t=t_str, c=c_str,
                detail=detail_html,
            )
        return lines_html

    def _post_bas_pull_summary(self, date_from, date_to, imported_lines,
                                gaps, cfg):
        """Post a chatter note summarising an action_pull_from_bas() run."""
        self.ensure_one()
        lines_html = self._format_commission_lines_html(imported_lines)

        def _sar(amount):
            return f'SAR {amount:,.2f}'

        warn_html = Markup('')
        if gaps['unmapped']:
            items = Markup('').join(
                Markup('<li><b>{name}</b> (BAS code {code}, {n} customer(s)) '
                       '— Sales: {s} | Collected: {c}</li>').format(
                    name=name or _('(no name in BAS)'), code=code or '—',
                    n=len(custs), s=_sar(s_amt), c=_sar(c_amt))
                for (code, name), (s_amt, c_amt, custs) in sorted(
                    gaps['unmapped'].items(),
                    key=lambda kv: -(kv[1][0] + kv[1][1])))
            warn_html += Markup(
                '<br/><b>⚠ BAS reps not linked to an employee — not '
                'credited to anyone:</b><ul>{items}</ul>'
                'Link them once under <i>Sales &amp; Collection ▸ BAS Sales '
                'Reps</i> (or mark them <i>No Commission</i>), then pull '
                'again.<br/>'
            ).format(items=items)
        if gaps.get('role'):
            items = Markup('').join(
                Markup('<li><b>{name}</b> (BAS code {code}, {n} customer(s)) '
                       '— Sales: {s} | Collected: {c}</li>').format(
                    name=name or code, code=code or '—', n=len(custs),
                    s=_sar(s_amt), c=_sar(c_amt))
                for (code, name), (s_amt, c_amt, custs) in sorted(
                    gaps['role'].items(),
                    key=lambda kv: -(kv[1][0] + kv[1][1])))
            warn_html += Markup(
                '<br/><b>⚠ Not credited — not that rep\'s role (salesman / '
                'collector on the profile) and the customer\'s own rep '
                'could not take it either:</b><ul>{items}</ul>'
            ).format(items=items)
        info = []
        if gaps['house']:
            for name, (s_amt, c_amt) in sorted(gaps['house'].items()):
                info.append(Markup(
                    '<li>House account <b>{name}</b> (no commission) — '
                    'Sales: {s} | Collected: {c}</li>').format(
                        name=name, s=_sar(s_amt), c=_sar(c_amt)))
        if info:
            warn_html += Markup('<br/><b>Not commissionable:</b>'
                                '<ul>{items}</ul>').format(
                items=Markup('').join(info))

        basis = Markup(
            '<i>Amounts excl. VAT ({vat}%) · sales: doc {sd} · collections: '
            'doc {cd} · ledgers {lg} · credited by {att} · target: {tm}. '
            'Customer detail: BAS Detail button.</i>'
        ).format(
            vat=f"{cfg['vat_rate']:g}", sd=', '.join(cfg['sales_doc_types']),
            cd=', '.join(cfg['collection_doc_types']),
            lg=', '.join(cfg['ledger_prefixes']) or _('all'),
            att=_('rep on each document') if cfg['attribution'] == 'document'
            else _("customer's current rep"),
            tm=_('due balance') if cfg['target_method'] == 'due_balance'
            else _('FIFO aging'))
        body = Markup(
            '<b>🔄 BAS Pull completed</b> '
            '(Period: {frm} – {to})<br/>{basis}<br/>'
            '<b>{n} line(s) updated/created:</b>'
            '<ul>{lines}</ul>'
            '{warn}'
        ).format(basis=basis,
            frm=date_from.strftime('%Y-%m-%d'),
            to=(date_to - timedelta(days=1)).strftime('%Y-%m-%d'),
            n=len(imported_lines),
            lines=lines_html,
            warn=warn_html,
        )
        self.message_post(body=body, subtype_xmlid='mail.mt_note')


class KswSalesCommissionLine(models.Model):
    _name = 'ksw.sales.commission.line'
    _description = 'KSW Sales / Collection Commission Line'
    _order = 'sheet_id, sequence, id'

    sheet_id = fields.Many2one(
        'ksw.sales.commission.sheet',
        required=True, ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict',
    )
    department_id = fields.Many2one(
        related='employee_id.department_id', store=True, readonly=True,
    )
    period = fields.Date(
        related='sheet_id.period', store=True, readonly=True,
    )
    role = fields.Selection(
        ROLE_SELECTION, compute='_compute_role_and_targets',
        store=True, readonly=False,
        help='Defaults to the salesperson profile role for the year. '
             'May be overridden per line if needed.',
    )
    partner_id = fields.Many2one(
        'res.partner', string='Client', ondelete='restrict',
        domain=[('customer_rank', '>', 0)],
        help='Optional. When set, client-specific commission rules '
             'will be matched first by the resolver.',
    )
    split_id = fields.Many2one(
        'ksw.salesperson.profile.client.split',
        string='Client Split', ondelete='set null',
        help='When set, this line covers only the clients defined in the '
             'split rule. The general line (split blank) receives the '
             'remaining totals.',
    )
    split_label = fields.Char(
        related='split_id.label', store=True, readonly=True,
        string='Split',
    )

    # Targets — computed from the salesperson profile, but editable so
    # that the accountant can override the auto-split for one month.
    target_sales = fields.Monetary(
        compute='_compute_role_and_targets', store=True, readonly=False,
    )
    target_collection = fields.Monetary(
        compute='_compute_role_and_targets', store=True, readonly=False,
    )

    x_commission_type = fields.Selection(
        ROLE_SELECTION, string='Type', readonly=True, copy=False,
        index=True,
        help='Set by Pull from BAS: which of the employee\'s commissions '
             'this line is. Salesman = customers he sells to and somebody '
             'else collects; Collector = customers he collects from and '
             'somebody else sold to; Salesman & Collector = both. One '
             'employee can have a line of each type, each with its own '
             'customers and rule.')
    x_sales_adjustment = fields.Monetary(
        string='Sales Adjustment',
        help='Added to the sales BAS gives (negative to reduce it) and kept '
             'across pulls — e.g. part of one BAS invoice that belongs to '
             'another rep or is not commissionable.')
    x_sales_adjustment_note = fields.Char(string='Sales Adjustment Reason')
    x_target_collection_adjustment = fields.Monetary(
        string='Target Adjustment',
        help='Added to the collection target BAS gives (negative to '
             'reduce it), e.g. a retention the client holds until the '
             'project ends. Kept across pulls.')
    x_target_adjustment_note = fields.Char(string='Adjustment Reason')

    achieved_sales = fields.Monetary(default=0.0)
    achieved_collection = fields.Monetary(default=0.0)

    sales_pct = fields.Float(
        string='Sales %', compute='_compute_commission', store=True,
    )
    collection_pct = fields.Float(
        string='Collection %', compute='_compute_commission', store=True,
    )

    sales_rule_id = fields.Many2one(
        'ksw.sales.commission.rule', readonly=True,
        compute='_compute_commission', store=True,
    )
    collection_rule_id = fields.Many2one(
        'ksw.sales.commission.rule', readonly=True,
        compute='_compute_commission', store=True,
    )
    combined_rule_id = fields.Many2one(
        'ksw.sales.commission.rule', readonly=True,
        compute='_compute_commission', store=True,
    )

    sales_commission_amount = fields.Monetary(
        compute='_compute_commission', store=True,
    )
    collection_commission_amount = fields.Monetary(
        compute='_compute_commission', store=True,
    )
    combined_commission_amount = fields.Monetary(
        compute='_compute_commission', store=True,
    )
    total_commission = fields.Monetary(
        compute='_compute_commission', store=True,
    )

    notes = fields.Char()

    # ------------------------------------------------------------------
    # Accountant-entered manual adjustments — one-off bonus/reward
    # additions and penalty/correction deductions on top of the
    # rule-computed commission. Mirrors the "Additional Commissions
    # Detail" / "Additional Deductions Detail" pattern already used on
    # annual leave / EOS approval (KSW_annual_leave hr_leave_commission_line
    # / hr_leave_deduction_line).
    # ------------------------------------------------------------------
    x_addition_line_ids = fields.One2many(
        'ksw.sales.commission.addition.line', 'line_id', copy=False,
        string='Addition Lines',
    )
    x_total_additions = fields.Monetary(
        compute='_compute_adjustment_totals', store=True,
    )
    x_deduction_line_ids = fields.One2many(
        'ksw.sales.commission.deduction.line', 'line_id', copy=False,
        string='Deduction Lines',
    )
    x_total_deductions = fields.Monetary(
        compute='_compute_adjustment_totals', store=True,
    )

    # ------------------------------------------------------------------
    # Sales-manager override
    # ------------------------------------------------------------------
    # When True, the rule's condition gate is bypassed in
    # ``_compute_commission`` and the tier ladder is consulted
    # unconditionally. Used when the sales manager grants an
    # exception to a salesperson who didn't meet the threshold.
    x_condition_override = fields.Boolean(
        string='Condition Overridden',
        copy=False, readonly=True,
        help='Set by a Sales Manager via the "Override Condition" '
             'button. While True, the commission is paid even when '
             'the rule\'s condition (threshold / formula) does not '
             'pass — using the tier ladder applied to the actual '
             'achievement percentage (or the lowest tier as a floor).',
    )
    x_override_by = fields.Many2one(
        'res.users', string='Overridden By',
        readonly=True, copy=False,
    )
    x_override_date = fields.Datetime(
        string='Override Date', readonly=True, copy=False,
    )
    x_override_reason = fields.Char(
        string='Override Reason', readonly=True, copy=False,
    )

    currency_id = fields.Many2one(
        related='sheet_id.currency_id', store=True, readonly=True,
    )

    # Uniqueness enforced in Python (see _check_unique_line) because
    # a DB UNIQUE on (sheet_id, employee_id, split_id) does not prevent
    # duplicate NULL split_id rows in PostgreSQL.

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.constrains('sheet_id', 'employee_id', 'split_id',
                    'x_commission_type')
    def _check_unique_line(self):
        for rec in self:
            domain = [
                ('sheet_id', '=', rec.sheet_id.id),
                ('employee_id', '=', rec.employee_id.id),
                ('id', '!=', rec.id),
                ('split_id', '=', rec.split_id.id if rec.split_id else False),
                ('x_commission_type', '=', rec.x_commission_type or False),
            ]
            if self.search_count(domain):
                if rec.split_id:
                    raise ValidationError(_(
                        "A split line '%(s)s' already exists for %(e)s on this sheet.",
                        s=rec.split_id.label, e=rec.employee_id.name,
                    ))
                else:
                    raise ValidationError(_(
                        "A commission line of this type for %(e)s already "
                        "exists on this sheet.", e=rec.employee_id.name,
                    ))
    @api.depends('employee_id', 'sheet_id.period', 'split_id',
                 'x_target_collection_adjustment', 'x_commission_type')
    def _compute_role_and_targets(self):
        Profile = self.env['ksw.salesperson.profile']
        for rec in self:
            if not rec.employee_id or not rec.sheet_id.period:
                rec.role = rec.role or 'sales'
                rec.target_sales = 0.0
                rec.target_collection = 0.0
                continue
            if rec.split_id:
                # Split lines take their role from the split definition.
                # Targets default to 0 (set manually or via import).
                rec.role = rec.split_id.role
                rec.target_sales = 0.0
                rec.target_collection = 0.0
            else:
                sales_t, coll_t, profile = Profile._get_targets(
                    rec.employee_id, rec.sheet_id.period,
                    ctype=rec.x_commission_type or None)
                if rec.x_commission_type:
                    # A BAS-typed line IS its type; the sales target sits
                    # on the line of the type the profile names.
                    rec.role = rec.x_commission_type
                    if profile and rec._sales_target_line_type(
                            profile.role) != rec.x_commission_type:
                        sales_t = 0.0
                else:
                    rec.role = (profile.role if profile
                                else (rec.role or 'sales'))
                rec.target_sales = sales_t
                rec.target_collection = max(
                    0.0, coll_t + rec.x_target_collection_adjustment)

    def _sales_target_line_type(self, profile_role):
        """Which of this employee's typed lines on the sheet carries his
        sales target: the one of his profile's role, else the nearest one
        that exists (Salesman & Collector, then Salesman, then Collector)."""
        self.ensure_one()
        types = set(self.sheet_id.line_ids.filtered(
            lambda l: l.employee_id == self.employee_id and not l.split_id
            and l.x_commission_type).mapped('x_commission_type'))
        types.add(self.x_commission_type)
        for t in (profile_role, 'both', 'sales', 'collect'):
            if t in types:
                return t
        return profile_role

    def _get_profile_rule(self, rec, kind):
        """Return the commission rule for this line, using the priority:

        For **split lines** (``split_id`` is set):
            The rule is always ``split_id.rule_id`` if its kind matches.
            No profile/scope resolution is done for split lines.

        For **general lines** (``split_id`` blank):
        1. Explicit rule set on the employee's salesperson profile
           (``sales_rule_id`` / ``collection_rule_id`` / ``combined_rule_id``).
        2. Auto-resolved rule via ``_resolve_rule`` scope matching
           (most-specific active rule for the employee/kind/client).
        """
        Rule = self.env['ksw.sales.commission.rule']
        # --- Split line: rule comes directly from the split definition ---
        if rec.split_id:
            split_rule = rec.split_id.rule_id
            if split_rule and split_rule.kind == kind:
                return split_rule
            return Rule  # kind mismatch → no commission for that kind
        # --- General line: profile explicit rule → scope resolver ---------
        if rec.employee_id and rec.sheet_id.period:
            Profile = self.env['ksw.salesperson.profile']
            profile = Profile.sudo().search([
                ('employee_id', '=', rec.employee_id.id),
                ('year', '=', fields.Date.to_date(rec.sheet_id.period).year),
                ('active', '=', True),
            ], limit=1)
            if profile and rec.x_commission_type:
                typed = profile.x_type_rule_ids.filtered(
                    lambda t: t.commission_type == rec.x_commission_type
                    and t.rule_id.kind == kind)[:1].rule_id
                if typed:
                    return typed
                if profile.x_type_rule_ids.filtered(
                        lambda t: t.commission_type == rec.x_commission_type):
                    # Rules are set per type for this employee and none of
                    # this kind: this type earns nothing of this kind.
                    return Rule
            if profile:
                explicit = {
                    'sales': profile.sales_rule_id,
                    'collection': profile.collection_rule_id,
                    'combined': profile.combined_rule_id,
                }.get(kind)
                if explicit:
                    return explicit
        # Fall back to generic scope-based resolution.
        return Rule._resolve_rule(rec.employee_id, kind, rec.partner_id)

    @api.depends('x_addition_line_ids.amount', 'x_deduction_line_ids.amount')
    def _compute_adjustment_totals(self):
        for rec in self:
            rec.x_total_additions = sum(rec.x_addition_line_ids.mapped('amount'))
            rec.x_total_deductions = sum(rec.x_deduction_line_ids.mapped('amount'))

    @api.depends('role', 'target_sales', 'target_collection',
                 'achieved_sales', 'achieved_collection',
                 'employee_id', 'partner_id', 'split_id', 'sheet_id.period',
                 'x_condition_override', 'x_total_additions', 'x_total_deductions')
    def _compute_commission(self):
        Rule = self.env['ksw.sales.commission.rule']
        for rec in self:
            sales_amt = 0.0
            coll_amt = 0.0
            comb_amt = 0.0
            sales_rule = Rule
            coll_rule = Rule
            comb_rule = Rule
            force = bool(rec.x_condition_override)

            rec.sales_pct = (
                (rec.achieved_sales / rec.target_sales) * 100.0
                if rec.target_sales else 0.0
            )
            rec.collection_pct = (
                (rec.achieved_collection / rec.target_collection) * 100.0
                if rec.target_collection else 0.0
            )

            if rec.role in ('sales', 'both'):
                sales_rule = self._get_profile_rule(rec, 'sales')
                if sales_rule:
                    sales_amt, _t, _p = sales_rule._evaluate(
                        rec.target_sales, rec.target_collection,
                        rec.achieved_sales, rec.achieved_collection,
                        employee=rec.employee_id, partner=rec.partner_id,
                        force_pass=force,
                    )
            if rec.role in ('collect', 'both'):
                coll_rule = self._get_profile_rule(rec, 'collection')
                if coll_rule:
                    coll_amt, _t, _p = coll_rule._evaluate(
                        rec.target_sales, rec.target_collection,
                        rec.achieved_sales, rec.achieved_collection,
                        employee=rec.employee_id, partner=rec.partner_id,
                        force_pass=force,
                    )
            if rec.role == 'both':
                comb_rule = self._get_profile_rule(rec, 'combined')
                if comb_rule:
                    comb_amt, _t, _p = comb_rule._evaluate(
                        rec.target_sales, rec.target_collection,
                        rec.achieved_sales, rec.achieved_collection,
                        employee=rec.employee_id, partner=rec.partner_id,
                        force_pass=force,
                    )
                    # When a combined rule fires, it replaces the
                    # standalone sales + collection payouts (admin
                    # picks one model or the other per employee).
                    if comb_amt:
                        sales_amt = 0.0
                        coll_amt = 0.0

            rec.sales_rule_id = sales_rule.id if sales_rule else False
            rec.collection_rule_id = coll_rule.id if coll_rule else False
            rec.combined_rule_id = comb_rule.id if comb_rule else False
            rec.sales_commission_amount = sales_amt
            rec.collection_commission_amount = coll_amt
            rec.combined_commission_amount = comb_amt
            rec.total_commission = (
                sales_amt + coll_amt + comb_amt
                + rec.x_total_additions - rec.x_total_deductions)

    # No _ksw_contributions and no CRUD sync: sales and collection are paid
    # outside the commission request, so nothing here reaches
    # ksw.commission.sheet.

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains('achieved_sales', 'achieved_collection',
                    'target_sales', 'target_collection')
    def _check_non_negative(self):
        for rec in self:
            if (rec.achieved_sales or 0.0) < 0 \
                    or (rec.achieved_collection or 0.0) < 0 \
                    or (rec.target_sales or 0.0) < 0 \
                    or (rec.target_collection or 0.0) < 0:
                raise ValidationError(_(
                    "Achieved and target amounts must be "
                    "zero or positive."))

    # ------------------------------------------------------------------
    # Sales-manager override actions
    # ------------------------------------------------------------------
    def _check_sales_manager(self):
        if self.env.su or self.env.user.has_group(
                'KSW_commissions.group_sales_commission_manager'):
            return
        raise UserError(_(
            "Only a Sales Manager can override the commission "
            "condition on a line."))

    def action_open_full_form(self):
        """Open this line on its own full page (target='current'), not as
        a dialog nested inside the sheet's ``line_ids`` — a one2many field
        (Addition/Deduction Lines) nested two levels deep inside that
        dialog hits an Odoo web-client bug ("View props should have a
        resModel key"). A real top-level page keeps the nesting to one
        level and renders fine.
        """
        self.ensure_one()
        return {
            'name': _("Sales/Collection Commission Line"),
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.sales.commission.line',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [
                (self.env.ref(
                    'KSW_commissions.view_ksw_sales_commission_line_form_full'
                ).id, 'form'),
            ],
            'target': 'current',
        }

    def action_open_override_wizard(self):
        """Open the override-reason wizard for the manager to capture
        a justification before flipping ``x_condition_override``.
        """
        self.ensure_one()
        self._check_sales_manager()
        return {
            'name': _("Override Commission Condition"),
            'type': 'ir.actions.act_window',
            'res_model': 'ksw.sales.commission.override.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_line_id': self.id,
            },
        }

    def action_revoke_override(self):
        """Clear an existing override (manager-only)."""
        self._check_sales_manager()
        for rec in self:
            if not rec.x_condition_override:
                continue
            prev_user = rec.x_override_by
            prev_reason = rec.x_override_reason
            rec.sudo().write({
                'x_condition_override': False,
                'x_override_by': False,
                'x_override_date': False,
                'x_override_reason': False,
            })
            rec.sheet_id.message_post(
                body=_(
                    "↩ Override revoked on line for <b>%(emp)s</b> by "
                    "<b>%(user)s</b>. Previous reason: <i>%(reason)s</i>",
                    emp=rec.employee_id.display_name or '',
                    user=self.env.user.name,
                    reason=prev_reason or '—',
                ),
                subtype_xmlid='mail.mt_note',
            )

    def _apply_override(self, reason):
        """Internal: stamp the override fields and chatter the sheet.
        Called by the wizard after capturing the reason.
        """
        self.ensure_one()
        self._check_sales_manager()
        self.sudo().write({
            'x_condition_override': True,
            'x_override_by': self.env.uid,
            'x_override_date': fields.Datetime.now(),
            'x_override_reason': reason or False,
        })
        self.sheet_id.message_post(
            body=_(
                "✔ Commission-condition <b>override granted</b> on line "
                "for <b>%(emp)s</b> by <b>%(user)s</b>.<br/>"
                "<b>Reason:</b> %(reason)s",
                emp=self.employee_id.display_name or '',
                user=self.env.user.name,
                reason=reason or '—',
            ),
            subtype_xmlid='mail.mt_note',
        )




