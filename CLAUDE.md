# KSW Odoo Project — Claude Context

## Second Brain — MANDATORY PROTOCOL

The project has an Obsidian knowledge base at **`~/KSW-Brain/`**. This is the authoritative map of everything built, in progress, and pending in this project.

**At the start of EVERY conversation:**
1. Read `~/KSW-Brain/Home.md` to understand current project state (what's done, in progress, backlog)
2. Read any relevant module or feature notes from `~/KSW-Brain/` before touching related code
3. Check `~/KSW-Brain/Gotchas & Patterns/Odoo 19 Pitfalls.md` before writing new models or views

**After completing ANY piece of work:**
1. Update the relevant note in `~/KSW-Brain/` (module note, feature note, or architecture note)
2. If a new feature was shipped: create a new `✅ Feature Name.md` in `~/KSW-Brain/Features/` and add it to the Done list in `Home.md`
3. If a new bug/gotcha was discovered: add it to `~/KSW-Brain/Gotchas & Patterns/Odoo 19 Pitfalls.md` and the July 2026 Audit Checklist if it's a recurring pattern
4. If a new module was created: add a note to `~/KSW-Brain/Modules/` and update the dependency chain in `~/KSW-Brain/Architecture/Dependency Chain.md`
5. If something moved from backlog to in-progress or done: update `Home.md` accordingly

**The second brain reduces ramp-up time every session. Keep it current.**

---

## Project Identity
- **Product**: Odoo 19 Community Edition, customised for Al-Kawthar Software (KSW)
- **Active branch**: `KSWDev` → merges into `19.0`
- **Author**: Mohammed Albadr / Mohammed Sadiq (`m.sadiq@alkawthersw.com`)

## Which environment to use — READ THIS FIRST
There are **two** Odoo configs/databases on this machine. **Only work against the
local dev one. Do not test against, or treat as authoritative, the other one.**

| | Use this (local dev) | Do not use as the target |
|---|---|---|
| systemd service | `odoo-dev.service` | — |
| Config file | `KSW_dev.conf` | `KSW.conf` |
| Database | `odoo_dev` | `KSWCO` |
| Port | `8070` | `8069` |
| Python | `/home/odoo/odoo19env/bin/python3.12` or project `.venv` | — |

Always run module upgrades/tests with `-c KSW_dev.conf`, e.g.:
```bash
python odoo-bin -c KSW_dev.conf -u KSW_payroll --stop-after-init
```
After any change, the running service needs an explicit restart to pick up
**Python code changes** (XML/data changes alone don't need it, but a restart is
the safe default since the web client also caches menus/views client-side):
```bash
sudo systemctl restart odoo-dev.service
```
Claude does not have a sudo session in this environment — this command must be
run by the user. Always ask for it after a change that touches `.py` files,
menus, or security/views, and don't assume it happened.

## Where the real work lives
```
custom_addons/KSW/          ← ALL custom development happens here
```
Everything else is read-only reference:
- `addons/` — Odoo 19 Community core (605 modules, 1.1 GB — do NOT edit)
- `odoo/` — Odoo framework source (do NOT edit)
- `custom_addons/cybrosys/` — third-party, do NOT edit
- `custom_addons/Odoo Mates - hr_payroll_community-19.0.1.0.1/om_hr_payroll/` — base payroll that KSW_payroll extends

## KSW Module Map
| Module | Purpose |
|--------|---------|
| `KSW_base_security` | Security groups & record rules for HR/Attendance |
| `KSW_working_schedule` | Work schedule fields on `hr.employee` |
| `KSW_attendance_leave` | Attendance-based leave tracking |
| `KSW_attendance_sheet` | Monthly attendance sheet for non-biometric employees |
| `KSW_attendance_report` | PDF monthly attendance report |
| `KSW_annual_leave` | Saudi-law annual leave: 21 days/yr (<5 yrs), 30 days/yr (≥5 yrs), auto-allocated daily; 6-step multi-approval chain with GM return-to-approver feature |
| `KSW_unpaid_leave` | Unpaid leave with 2-step approval + attendance integration |
| `KSW_leave_approval` | 2-step time-off approval: Direct Manager → HR Manager |
| `KSW_deduction` | Employee deductions (loans, penalties, advances) |
| `KSW_commissions` | Monthly commissions & allowances for non-biometric employees |
| `KSW_payroll` | Full payroll: extends `om_hr_payroll`, biometric attendance deduction, payslip runs, bank export; `ksw.payslip.run.bank.total` stores per-bank NET totals on the batch |

## Dependency Chain
```
KSW_base_security
KSW_working_schedule
KSW_attendance_leave  ←  KSW_annual_leave
                      ←  KSW_unpaid_leave
                      ←  KSW_attendance_sheet  ←  KSW_attendance_report
                                               ←  KSW_commissions
KSW_leave_approval
KSW_deduction
om_hr_payroll  ←  KSW_payroll (depends on all above)
```

## Key Domain Rules (Saudi Labour Law)
- Annual leave: **21 days/year** for first 5 years, **30 days/year** after 5 years
- Leave duration counts **calendar days including weekends**
- Payslip computation is **blocked** if an employee has an unconfirmed annual leave return
- Attendance deduction rule: `ATTDED` worked-day line feeds into salary rule

## Coding Conventions
- Use `_inherit` to extend existing Odoo models — never create standalone replacements
- Follow Odoo ORM conventions: `@api.depends`, `@api.constrains`, `store=True` for computed fields that need search
- XML ids: prefix with module technical name, e.g. `KSW_payroll.action_payslip_tree`
- Security: always add model access in `security/ir.model.access.csv` and group rules in `security/security.xml`
- Tests go in `tests/` with `__init__.py` importing them; use `TransactionCase`
- Commit prefix: `[ADD]`, `[FIX]`, `[IMP]`, `[REF]`, `[REM]`

## What NOT to do
- Never edit files outside `custom_addons/KSW/`
- Never suggest `pip install` — use `.venv` already at project root
- Don't rewrite entire files when a targeted `_inherit` override suffices
- Don't add demo data unless explicitly asked

## Running / Testing
Always use `KSW_dev.conf` (see environment table above) — never `KSW.conf`.
```bash
# Start server (normally already running as odoo-dev.service — don't start a
# second one on the same port; use the upgrade/shell commands below instead)
python odoo-bin -c KSW_dev.conf

# Run tests for a module
python odoo-bin -c KSW_dev.conf --test-enable -u KSW_payroll --stop-after-init

# Upgrade a module
python odoo-bin -c KSW_dev.conf -u KSW_annual_leave --stop-after-init

# Inspect/verify via shell (don't call env.cr.commit() unless you intend to
# persist a real fix — without it, all writes roll back when the process exits)
python odoo-bin shell -c KSW_dev.conf --no-http
```

## Preferences
- Responses should be concise — show diffs, not full file rewrites
- When referencing Odoo core behaviour, cite the model/method name (e.g. `hr.leave._check_approval_update`) rather than copying source
- Prefer one targeted fix over restructuring surrounding code

## Odoo gotchas hit in this project (read before touching security/menus/views)

These caused real, confusing breakage across several sessions while building the
KSW_payroll access-tier feature. Know them before doing similar work again.

1. **`<menuitem>` partial overrides silently wipe `parent_id` and `name` if you
   don't repeat them.** `_tag_menuitem` in `odoo/tools/convert.py` defaults
   `parent_id` to `False` and `name` to the literal `id` string whenever those
   attributes aren't present on the tag — even when you only meant to change
   `groups=`. Result: the menu silently detaches into its own top-level app, or
   its label turns into a raw `module.xml_id` string. **Always repeat
   `parent="..."` and `name="..."` (matching the original) on every
   `<menuitem>` override, even a one-attribute change.** This bug reappeared
   identically across multiple rounds before being caught.

2. **A `<record>` override targeting a record created inside another module's
   `<data noupdate="1">` block is silently ignored on every upgrade after the
   first install.** It only works the very first time the two modules install
   together (`mode='init'`). On every `-u` upgrade afterward, Odoo's
   `_load_records` skips the write (see `odoo/orm/models.py`, the
   `update and d_noupdate` check). Symptom: the override XML loads with no
   error, but the field never actually changes.
   **Fix: use `<function model="..." name="write">` instead of `<record>`.**
   It calls `write()` directly and isn't subject to that gate (as long as the
   `<function>` tag itself isn't inside a `noupdate="1"` block). KSW_deduction
   already uses this pattern for extending `base.group_user.implied_ids` —
   reuse it for any "extend another module's existing group/record" need.

3. **Deleting/reverting a file does not undo what it already wrote to the
   database.** Groups, rules, access rows, and `<function>`-driven writes
   persist until explicitly unlinked/reverted via the ORM, independent of the
   module's current files. After removing a security XML file, check the
   database (`ir.model.data` for that module + the target records it touched)
   before assuming things are back to normal — `git checkout` is not enough.

4. **XML comments cannot contain a literal `--`.** Breaks `lxml` parsing with
   `XMLSyntaxError: Double hyphen within comment`. Use `:` or "and" instead of
   `--` as a dash inside any `<!-- -->` block. Hit repeatedly this session.

5. **Field-level `groups="..."` restrictions raise `AccessError` inside
   computes and QWeb reports, not just in the UI.** Any field with `groups=`
   set (e.g. `hr.version.wage`, `hr.employee.bank_account_ids`,
   `hr.version.identification_id` — the latter two delegated onto
   `hr.employee` via `_inherits`) will blow up the moment a user without that
   group triggers a `@api.depends` compute or a report template that reads it
   — even if the user has full row-level (`ir.rule`) access to the parent
   record. Fix by wrapping just that read with `.sudo()` (see
   `KSW_payroll/models/hr_payslip.py::_compute_wage_rates` and
   `KSW_payroll/report/report_payslip_deduction_templates.xml` for the
   pattern). When adding a new read-only access tier, grep the views/reports/
   computes that tier will reach for any `groups=` fields first, rather than
   waiting for the user to hit them one at a time.

6. **List-view header buttons default to `display="selection"`** (only
   rendered once a row is checked — see `control-panel-selection-actions` in
   `addons/web/static/src/views/list/list_controller.xml`). For an
   always-visible action button (e.g. "Request a Loan" on the My Loans list),
   set `display="always"` on the `<button>` inside `<header>`. No JS needed.

7. **Before testing any access-rights change, confirm you're hitting the
   right database** (see the environment table above). An entire round of
   "this fix doesn't work" in this project turned out to be testing
   `KSW.conf`/`KSWCO` while the user was looking at `KSW_dev.conf`/`odoo_dev`.

8. **`self.env.with_user(user)` does not exist in Odoo 19.** Use
   `Model.with_user(user)` on a recordset/model instead:
   ```python
   # WRONG:
   self.env.with_user(user)['ksw.deduction'].search(...)
   # RIGHT:
   self.env['ksw.deduction'].with_user(user).search(...)
   ```
   This bit us in test helpers that tried to switch users on the env object.

9. **Odoo 19's `assertRaises` does not accept a tuple of exceptions.**
   Standard Python `unittest.assertRaises((A, B))` works, but Odoo 19's
   override of `assertRaises` calls `issubclass(exception, AccessError)` where
   `exception` is your first arg — a tuple raises `TypeError` there. Always
   catch a single exception class per `assertRaises` block, e.g.
   `self.assertRaises(UserError)` not `self.assertRaises((UserError, AccessError))`.
   If the code can raise either, write two separate tests or catch in a try/except.

10. **When running tests while `odoo-dev.service` is already on port 8070, use
    `--http-port=18070`** (or any free port) instead of `--no-http`. The
    `--no-http` flag does not suppress the port-in-use check in this build;
    specifying an alternate port is the reliable workaround:
    ```bash
    python odoo-bin -c KSW_dev.conf --http-port=18070 --test-enable -u KSW_deduction --stop-after-init
    ```

11. **`message_post()` on a document fails when the calling user lacks
    `base.group_user` (the internal-user group).** Custom groups
    (e.g. `group_installment_edit`, `group_loan_acc`) that don't imply
    `base.group_user` cannot create `mail.message` records. Pattern to follow:
    once you have already verified the caller is authorised, use
    `record.sudo().message_post(...)` for the chatter write. The `sudo()` is
    safe here because auth was checked before entering the block. Same applies
    to `record.sudo().write({'state': 'completed'})` when auto-completing a
    workflow step that any authorised user should be able to trigger regardless
    of their record-rule scope (e.g. accounting user marking an HR-type
    deduction as completed after a manual payment).

12. **Python `create()` override raises `UserError` before ORM ACL raises
    `AccessError`.** If your custom `create()` has an early privilege guard that
    calls `raise UserError(...)`, that fires before `super().create()` reaches
    `check_access('create')`. Test expectations should match what actually
    reaches the caller: `assertRaises(UserError)` not `assertRaises(AccessError)`.

13. **Chatter HTML bodies must use `Markup(template) % dict(...)` — never
    `Markup(''.join([plain_strings]))`.** This is the canonical pattern for all
    `message_post(body=...)` calls in this codebase:
    ```python
    # CORRECT — markupsafe auto-escapes every substituted value
    body = Markup(
        '<strong>✅ Step N — Title</strong><br/>'
        '<b>By:</b> %(user)s<br/>'
        '<b>Amount:</b> %(amt).2f SAR'
    ) % {'user': self.env.user.name, 'amt': rec.amount}
    # Conditional append: also use Markup % dict(...)
    if some_field:
        body += Markup('<b>Note:</b> %(note)s<br/>') % {'note': some_field}
    # Sub-expressions that are themselves Markup are NOT double-escaped
    note_part = Markup(' (%(n)s)') % {'n': ml.manual_note} if ml.manual_note else Markup('')
    body += Markup('• %(label)s%(note)s<br/>') % {'label': ml.display_name, 'note': note_part}
    record.message_post(body=body, subtype_xmlid='mail.mt_note')

    # WRONG — Markup(''.join(...)) marks already-tainted strings as safe: XSS
    body = ['<b>By:</b> %s<br/>' % user.name]   # raw %s, not escaped
    record.message_post(body=Markup(''.join(body)))  # ← never do this
    ```
    The safe pattern already appears in `action_dm_approve` / `action_hr_approve`
    in `KSW_deduction` as the reference. All seven XSS sites across
    `ksw_deduction.py`, `ksw_deduction_line.py`, `KSW_annual_leave/hr_leave.py`,
    and `KSW_unpaid_leave/hr_leave.py` were fixed in June 2026 — don't reintroduce
    the list-join pattern.

14. **Every computed field whose result varies per user needs
    `@api.depends_context('uid')`.** Without it, Odoo's ORM cache returns the
    first user's result to every subsequent caller in the same request.
    ```python
    @api.depends_context('uid')          # ← required when result differs per user
    @api.depends('employee_id', 'employee_id.parent_id.user_id')
    def _compute_x_can_dm_approve(self):
        uid = self.env.uid
        ...
    ```
    Fields already using this pattern: `x_can_edit_installments`,
    `x_can_submit`, `x_can_dm_approve` (in `ksw.deduction`).
    Apply it to any Boolean "can the current user do X?" field.

15. **Action methods that change state must have a server-side privilege check.**
    View-level `invisible=` is purely cosmetic — any user with ORM write access
    can call the method via RPC. Pattern: check the group (or ownership) at the
    top of the method and raise `UserError` before any write:
    ```python
    def action_confirm_return_manager(self):
        user = self.env.user
        for leave in self:
            ...
            if not self.env.su:
                is_hr = user.has_group('KSW_annual_leave.group_annual_leave_hr')
                is_manager = (leave.employee_id.leave_manager_id == user)
                if not (is_hr or is_manager):
                    raise UserError('Only the leave manager or HR can do this.')
    ```
    See also the existing `self._check_group(...)` helper in
    `KSW_annual_leave/models/hr_leave.py` for the approval-step pattern.

16. **`_check_group` does NOT check `self.env.su` — use `with_user()`, not just `sudo()`.** The KSW `_check_group(xmlid, message)` helper calls `self.env.user.has_group(xmlid)` unconditionally. This means `record.sudo()` alone is insufficient if the effective user (from `with_user`) lacks the group. Pattern for tests and server code:
    ```python
    # WRONG — sudo() doesn't give user_hr the group_annual_leave_hr group
    leave.sudo().action_hr_approve()
    # RIGHT — set the authorised user first, then widen record-rule scope with sudo
    leave.with_user(user_hr).sudo().action_hr_approve()
    ```
    Applies equally to `action_dm_approve`, `action_gm_initial_approve`,
    `action_acc_approve`, `action_gm_final_approve`.

17. **In Odoo 19, `hr.leave` is created directly in `confirm` state — `action_confirm()` does not exist on the base model.** The field has `default='confirm'`. Any test or override that calls `leave.action_confirm()` or `super().action_confirm()` will raise `AttributeError: 'super' object has no attribute 'action_confirm'`. Move post-create side effects (DM notifications, etc.) into the `create()` override directly.

18. **`action_gm_final_approve` does NOT directly validate the leave (state → validate). It moves to `pending_employee_signature`.** The Odoo `_action_validate` call only happens after `action_employee_confirm_signature()` — now called by HR only (changed July 2026). Tests that assert `leave.state == 'validate'` after `action_gm_final_approve` will fail. Add the HR confirmation step with a stub attachment and `with_user(user_hr).sudo()` (see KSW_annual_leave architecture section below).

19. **`_generate_weekend_records` (cybrosys) crashes for night-shift employees.**
    The upstream code extracts only `.hour`/`.minute` from `ref_schedule['end']`
    and places both check_in and check_out on the same `grant_day`. For overnight
    schedules (e.g. 22:00–06:00) `get_employee_day_schedule` already advances
    `end` by +1 day, but `_generate_weekend_records` discards that offset, making
    `check_out < check_in` → `ValidationError`. The fix — `if sched_end <=
    sched_start: sched_end += timedelta(days=1)` — is applied as an override in
    `KSW_attendance_leave/models/biometric_attendance_sync.py`. Never add a bare
    call to the upstream `_generate_weekend_records` for employees with night
    schedules; always go through the KSW override (which is the default once the
    module is loaded).

    **Root cause of missing old Fridays in KSWCO (June 2026):** "Generate All
    Absences" runs as a background cron (`_run_generate_all_absences`). It calls
    `_generate_weekend_records` for all biometric employees. If that run happened
    before the device sync imported the full historical attendance, the adjacent-
    workday check (`day_before in attended_dates or day_after in attended_dates`)
    found no qualifying neighbour and silently skipped the weekend day — permanently,
    because the function only creates records that don't already exist. Fix: re-run
    "Generate All Absences" (or call `sync._generate_weekend_records(employees,
    date_from, date_to)` directly in a shell) once all punch data is present.

50. **A DB-level FK cascade still fires a compute's dependency trigger — a
    field that must survive a related record's deletion cannot be a
    `compute=` on a path through that relation, even with `ondelete='set
    null'`.** `hr.leave.attendance.line.attendance_id` used `ondelete='cascade'`,
    so deleting an `hr.attendance` row (the sanctioned repair: delete +
    re-download a period) destroyed the line that recorded an HR-approved
    late/early-leave excuse's `accepted_minutes` — and the m2m junction row
    (`hr_leave_attendance_rel`, always `ON DELETE CASCADE` on both columns,
    can't be changed) went with it regardless. The re-downloaded punch gets a
    new id; nothing re-attaches the leave to it, so the excuse silently
    stopped applying and its deduction came back (KSWCO leave 4839, employee
    9940, 5 Jul 2026 — confirmed via `hr_leave_attendance_rel`/
    `hr_leave_attendance_line` both empty for a leave still `state='validate'`).
    Switching `attendance_id` to `ondelete='set null'` was only half the fix:
    `Model.unlink()` calls `self.modified(self._fields, before=True)`
    *before* the DB delete, which walks the dependency graph and re-triggers
    every `compute=` reachable through the relation — so a `date = fields.Date
    (compute='_compute_date', ..., depends='attendance_id.check_in')` field
    got recomputed to `False` the moment `attendance_id` changed, even though
    the column value itself would otherwise have survived untouched.
    **Fix, two parts:**
    ```python
    # hr_leave_attendance_line.py — no compute, set once at creation
    attendance_id = fields.Many2one('hr.attendance', ondelete='set null')  # was 'cascade'
    date = fields.Date()  # was compute='_compute_date', store=True

    # hr_leave.py::_generate_attendance_lines — the only place lines are built
    new_lines.append((0, 0, {
        'attendance_id': att_id, 'date': check_date, ...   # date added
    }))

    # hr_attendance.py — re-attach on the next real punch for that employee+date
    def _relink_attendance_issue_lines(self):
        orphans = Line.search([
            ('attendance_id', '=', False), ('date', '=', att.check_in.date()),
            ('leave_id.employee_id', '=', att.employee_id.id),
            ('leave_id.state', '=', 'validate'),
        ])
        orphans.write({'attendance_id': att.id})
        orphans.leave_id.sudo().write({'x_attendance_ids': [(4, att.id)]})
    ```
    called from `hr.attendance.create()` for every new non-absent record.
    **Rule: any field whose only job is to survive a related record's
    deletion and let you re-link later must be a plain field set explicitly
    at creation — never `compute=` on a path through the relation being
    deleted, even a stored one, even with `ondelete='set null'` on the FK.**
    A day-based attendance-issue leave type (full-day absence excuse, no
    `hr.leave.attendance.line` at all — just the bare `x_attendance_ids` m2m)
    has the same exposure with *no* surviving row to relink from at all;
    `KSW_attendance_leave._check_absence_for_date` already skips recreating
    the absence when such a leave covers the date, so that day just goes
    missing from the sheet instead of resurfacing a deduction — a different,
    not-yet-fixed failure mode, flagged for follow-up rather than fixed here.

## KSW_deduction architecture notes

### managed_by field
`ksw.deduction.type.managed_by` (`Selection`: `'acc_data_entry'` / `'accounting'`,
default `'acc_data_entry'`) controls who owns the type: who may create it and
manually close its installments outside payroll.

**August 2026 — non-loan deductions moved from HR to accounting.** The value
`'hr'` became `'acc_data_entry'` and the group `group_hr_deduction_officer`
("HR Officer (Non-Loan)") became `group_acc_data_entry` ("Accounting Data Entry
(Non-Loan)"). Both renames, including the group's xml id, are handled by
`migrations/19.0.1.1.0/pre-migrate.py` — group membership is preserved.

| Type | managed_by |
|------|-----------|
| Loan | `accounting` (label "Accounting (Loans)") |
| Salary Advance | `acc_data_entry` |
| Gov. Penalty | `acc_data_entry` |
| Internal Penalty | `acc_data_entry` |

`ksw.deduction.managed_by` is a `store=True` related field that mirrors the type's
value. Stored so record rules can filter on it without a subquery.

### Installment privilege matrix

| Group | Non-loan (`acc_data_entry`) | Loans (`accounting`) |
|-------|------------------------------|----------------------|
| `group_acc_data_entry` | create, mark paid, edit schedule, wizard | not visible |
| `group_deduction_officer` | **read-only** | create/edit (loan rules apply) |
| `group_loan_hr` | **read-only** | see + approve loans |
| `group_deduction_manager` | full (administrator) | full |
| `group_installment_edit` | mark paid, edit schedule, wizard | mark paid, edit schedule, wizard |

`x_can_edit_installments` is a non-stored `@api.depends_context('uid')` computed
Boolean on `ksw.deduction` that reflects this matrix for the current user. The view
uses it for `invisible=` on the installments tab; Python `create()`/`write()` guards
enforce it at the ORM level.

The read-only tier is enforced by splitting the officer record rule in two —
`ksw_deduction_rule_officer` (read, `[(1,'=',1)]`) and
`ksw_deduction_rule_officer_write` (write/create/unlink,
`[('managed_by','!=','acc_data_entry')]`) — plus
`ksw_deduction_rule_non_loan_readonly` for `group_loan_hr`.
`ksw.deduction.create()` also calls `_check_acc_data_entry_ownership()` so an HR
user gets an explanatory `UserError` instead of a bare `AccessError`.

### action_mark_line_paid
`ksw.deduction.line.action_mark_line_paid()` stamps a single pending line as paid
in-place (`state='paid'`, `is_manual=True`, `manual_by`, `manual_date`). Posts a
chatter note on the parent deduction. Auto-completes the deduction if all lines are
now paid. Uses `ded.sudo()` for both `message_post` and `write({'state': 'completed'})`
because the accounting user may lack write scope to HR-managed deductions via record
rules, yet is authorised to trigger auto-completion (auth is checked before the loop).

### ksw.loan.payment.wizard
`wizard/ksw_loan_payment_wizard.py` — TransientModel for partial or full payments
outside payroll. **Not loan-only** (widened July 2026): the **Record Payment** header
button shows on any *active* deduction that has pending installments. Both the
button's `invisible=` and the `action_confirm` guard read
`ksw.deduction.x_can_edit_installments` — the same matrix as the Installments tab:
accounting with Loan Modification = Full (`group_installment_edit`) settles any type;
`group_acc_data_entry` (and `group_deduction_manager`) settle
`managed_by='acc_data_entry'` types only. HR-side roles cannot settle anything.
The button's `groups=` list is only a coarse pre-filter and **must stay a superset** of
the groups that compute can grant, or a user who passes the button filter will be
missing the field from `fields_get()` and crash (gotcha #31). The model keeps its
historical `ksw.loan.payment.wizard` name.

- **Full payment** (`payment_amount == total_outstanding`): stamps all pending lines
  as paid in-place via O2M `(1, id, vals)` commands on the parent write. No new
  line added.
- **Partial payment**: distributes remaining balance equally across pending lines
  (last one absorbs rounding residue), then adds one new manual paid line for the
  payment amount via `(0, 0, vals)`.
- Both paths use `ded.sudo().write({'line_ids': commands})` so the parent write
  sets `_skip_installment_total_check=True` atomically during O2M processing and
  validates the total constraint once at the end.
- Chatter writes use `ded.sudo().message_post(...)` (see gotcha #11).

### Record rule: group_loan_hr sees non-loan deductions (read-only)
`ksw_deduction_rule_non_loan_readonly` (in `KSW_deduction/security/security.xml`)
ORs with the existing loan-approvers rule so `group_loan_hr` users **see**:
loans (`is_loan=True`) **OR** non-loan deductions (`managed_by='acc_data_entry'`),
but may only **write** the loans. The `managed_by` field must be `store=True` on
`ksw.deduction` for this domain to work.

Note `group_loan_hr` used to *imply* the non-loan officer group; that implication
lived on in databases even after the file stopped declaring it (gotcha #3), so the
XML now uses `(6, 0, [...])` on its `implied_ids` to actively remove it.

### Employee detail mirrors (read-only)
Three non-stored **sudo computes** (`_compute_employee_identifiers`, converted from
`related=` in August 2026 so non-HR deduction roles can read them) on
`ksw.deduction` surface employee identifiers directly on the deduction form
(Employee group) without a separate lookup:

| Field | Source | Label |
|-------|--------|-------|
| `x_emp_employee_no` | `employee_id.x_employee_no` (KSW_payroll) | Employee No. |
| `x_emp_ssnid` | `employee_id.ssnid` (hr.version via `_inherits`) | SSN No. |
| `x_emp_loan_acc_no` | `employee_id.x_loan_acc_no` (KSW_deduction/hr_employee) | Loan Acc No. in BAS |

All three carry `groups='hr.group_hr_user'` (inherited from the source fields).
Do **not** store them — they are display-only and need no column.

### Payroll impact summary fields
Two fields on `ksw.deduction` summarise the employee's payroll picture for HR
approvers. Both are shown in a dedicated **Payroll Impact** group on the form
(HR users only) and in the kanban card:

- **`x_emp_monthly_total`** — non-stored `related` → `employee_id.x_deduction_monthly_total`.
  Shows the sum of all pending installments for the current month across every
  active deduction for this employee (including this one). Computed in
  `KSW_deduction/models/hr_employee.py::_compute_deduction_count`.

- **`x_gross_salary`** — non-stored `compute='_compute_gross_salary'` (no model-level
  `groups=`). Reads `employee_id.sudo().current_version_id.wage` (sudo required because
  `wage` is group-restricted). Model-level `groups=` was intentionally omitted: the
  compute already protects the underlying data via `sudo()`, and adding `groups=` at
  the model level would cause an OWL "field is undefined" crash for loan approvers who
  see view elements with `invisible=` expressions referencing this field (see gotcha
  #31). View-level groups on the form "Payroll Impact" section, list column, and kanban
  card restrict visibility to HR users and all deduction-management/loan-approval groups.

## KSW_annual_leave: multi-step approval chain

### Full chain
`emp → pending_dm → pending_hr → pending_gm_initial → pending_acc →
pending_gm_final → pending_employee_signature → approved`

Field: `x_annual_approval_state` (Selection) on `hr.leave`.

### Per-step action methods
| State leaving | Method | Group required |
|---|---|---|
| `pending_dm` | `action_dm_approve` | leave manager of the employee (or any HR) |
| `pending_hr` | `action_hr_approve` | `group_annual_leave_hr` |
| `pending_gm_initial` | `action_gm_initial_approve` | `group_annual_leave_gm` |
| `pending_acc` | `action_acc_approve` | `group_annual_leave_acc` |
| `pending_gm_final` | `action_gm_final_approve` | `group_annual_leave_gm` |
| `pending_employee_signature` | `action_employee_confirm_signature` | **HR only** (`group_annual_leave_hr`) |

Step 6 changed in July 2026: it was previously confirmed by the employee/DM. It is
now HR-only. The notification after GM final approval goes to the HR group (not the
employee or DM). The state label is "Pending HR Confirmation".

`_check_group(xmlid, message)` raises `UserError` when the calling user lacks the
group. **It does NOT check `self.env.su`.** So `record.sudo()` alone is not enough
to bypass the check — use `record.with_user(authorised_user).sudo()` in tests.

### Odoo 19: no `action_confirm` on `hr.leave`
Odoo 19's `hr.leave` has `default='confirm'` — leaves are created directly in
`confirm` state. There is no `action_confirm()` method on the base model.
The KSW `create()` override calls `_notify_pending_approvers(leave, 'pending_dm')`
directly after writing `x_annual_approval_state = 'pending_dm'`.
Do **not** add `action_confirm()` overrides or test code that calls it.

### HR confirmation step (Step 6)
`action_gm_final_approve` moves the leave to `pending_employee_signature`
(NOT directly to `validate`). The Odoo `state → 'validate'` only happens after
`action_employee_confirm_signature()` is called **by an HR user**. That method
requires at least one attachment on `x_attachment_ids`. In tests, create a stub
attachment and call via `with_user(user_hr).sudo()`:
```python
att = self.env['ir.attachment'].sudo().create({
    'name': 'test_signed_form.pdf',
    'datas': base64.b64encode(b'stub'),
    'res_model': 'hr.leave', 'res_id': leave.id,
})
leave.sudo().write({'x_attachment_ids': [(4, att.id)]})
leave.with_user(user_hr).sudo().action_employee_confirm_signature()
```
`x_can_sign` (gate field for the button) is True only for HR users at this step.

### GM Return-to-Approver wizard
`ksw.gm.return.approver.wizard` (in `KSW_annual_leave/wizard/`) lets the GM
return a leave to an earlier approver instead of refusing it outright.

- **Valid return targets** — only from the two GM steps:
  - From `pending_gm_initial`: `pending_dm` or `pending_hr`
  - From `pending_gm_final`: `pending_dm`, `pending_hr`, `pending_gm_initial`,
    or `pending_acc`
- **Stamp clearing**: returning to state X clears that stamp and all later ones;
  earlier stamps are preserved. `_CLEAR_STAMPS` dict in the wizard defines this.
- **Gate field**: `x_can_gm_return` (Boolean, `@api.depends_context('uid')`) is
  True only for GM users at the two GM steps. Controls button visibility in the
  form and is the correct guard to read in XML `invisible=` expressions.
- **Inbox notification**: `_notify_return()` posts with `mail.mt_comment` and
  sets `partner_ids` so the target approver (group or DM user) gets an Odoo
  inbox notification containing the GM's reason text.
- **Test helper pattern**: `_advance_to(leave, target)` in the tests is
  resume-aware — it skips steps already past the current state. This lets you
  call it multiple times on the same leave with increasing targets.

20. **`editable="0"` is not a valid value for the `<list>` editable attribute.** Odoo 19 only accepts `"top"` or `"bottom"`. Using `editable="0"` raises a `ParseError` on module upgrade. For a read-only embedded list, simply omit the `editable` attribute (or set `readonly="1"` on the parent `<field>`).

21. **Inserting elements after a field inside a `<group>` puts them *inside* that group, breaking the grid layout.** In the `hr.payslip.run` base form, `credit_note` lives inside a `<group col="4">`. Any `<xpath expr="//field[@name='credit_note']" position="after">` block adds its children to that same 4-column group, which misaligns the Period/Credit Note row. To place content *outside* the group (at sheet level), use a separate xpath targeting a sibling that is outside the group — e.g. `<xpath expr="//field[@name='slip_ids']" position="before">`.

22. **`hr.payslip.line` `digits=(16, 0)` override causes a 1-SAR NET display gap when deduction inputs are fractional.** KSW_payroll overrides `hr.payslip.line.amount`, `quantity`, and `total` to `fields.Float(digits=(16, 0))`, storing all amounts as integers. The base engine (`om_hr_payroll`) accumulates category sums using `currency.round()` (SAR = 2 dp), so a 87.5 SAR loan installment enters `categories.DED` as -87.5, giving `NET = 6153.5`, which Python's banker's rounding stores as 6154. But the displayed `KSW_DEDUCTIONS` line is -88 (87.5 rounded to nearest integer), so the user sees `6850 − 609 − 88 = 6153 ≠ NET 6154`.
    **Fix applied in `KSW_payroll/models/hr_payslip.py`:** After `super().compute_sheet()`, a post-processing block re-derives `NET = GROSS.amount + Σ(DED category line amounts)` using the already-rounded integer stored values. This guarantees the displayed numbers are always arithmetically consistent.
    **Test coverage:** `KSW_payroll/tests/test_net_rounding_consistency.py` — five tests (integer inputs, 87.5 fractional, two fractional inputs, round-down fractional, no KSW_DED inputs).

23. **When testing "notification NOT sent to user X", snapshot `message_ids.ids` before the action, then filter to only new messages.** `leave.message_ids` contains ALL chatter history. If user X was notified at an earlier step (e.g. the DM is in `partner_ids` of the step-1 creation message), a bare `leave.message_ids.filtered(lambda m: x_partner in m.partner_ids)` will find that old message and give a false positive. Pattern:
    ```python
    existing_ids = leave.message_ids.ids
    leave.with_user(some_user).sudo().action_something()
    new_msgs = leave.message_ids.filtered(lambda m: m.id not in existing_ids)
    self.assertFalse(new_msgs.filtered(lambda m: x_partner in m.partner_ids))
    ```
    Hit during `TestHrConfirmationStep.test_gm_final_does_not_notify_dm` — the DM
    WAS in the step-1 notification but not in the step-6 one; the test was wrong, not
    the code.

24. **Odoo 19 rewrites `=`/`!=` boolean conditions to `in`/`not in` (with an
    `OrderedSet` value) BEFORE calling a field's `search=` method** (see
    `odoo.orm.domains._operator_equal_as_in`; core search methods check
    `if operator != 'in': return NotImplemented`). A guard like
    `if operator not in ('=', '!=') ...: return []` therefore ALWAYS takes the
    early-return path, and returning `[]` means "match every record" — the
    filter silently shows everything with no error. This bug shipped in
    `KSW_annual_leave._search_is_pending_my_action` and was copied into
    KSW_deduction before being caught (July 2026; both now fixed). Pattern:
    ```python
    if operator in ('in', 'not in'):
        positive_wanted = (operator == 'in') == any(value)
    elif operator in ('=', '!='):
        positive_wanted = (operator == '=') == bool(value)
    else:
        return NotImplemented
    ```
    Related: a dotted domain path like `('employee_id.parent_id.user_id', '=',
    False)` does NOT match records whose intermediate `parent_id` is null —
    OR it with `('employee_id.parent_id', '=', False)` explicitly.

### `requires_allocation` in tests
Must be `False` (Python bool), **not** `'no'` (truthy string). The string `'no'`
evaluates as True and triggers `_check_validity`'s allocation guard even when no
allocation is needed. All test leave types that do not require allocation must use
`'requires_allocation': False`.

25. **`name_get()` is dead in Odoo 19 — use `_compute_display_name` instead.**
    The ORM never calls `name_get()` anymore; any override is silently ignored and
    display names fall back to the `_name,id` generic form. This breaks chatter
    references, wizard selects, and any `display_name` read. Pattern:
    ```python
    # WRONG — dead code in Odoo 19
    def name_get(self):
        return [(r.id, 'Jan 2026') for r in self]

    # CORRECT
    def _compute_display_name(self):
        for r in self:
            r.display_name = 'Jan 2026'
    ```
    Found in `ksw.deduction.line` (month/year labels). Check every model for
    `name_get` before adding display-name logic to a new model.

26. **Approve actions need the same server-side group guard as their refuse
    counterparts — approve and refuse are NOT asymmetric in their access
    requirements.** `_do_refuse` in `ksw_deduction.py` correctly enforces
    `has_group('group_loan_hr/acc/gm')` per step. But `action_hr_approve`,
    `action_acc_approve`, and `action_gm_approve` had no such check — any user
    with write access on `ksw.deduction` could walk a loan through the full chain
    via RPC. Rule: **every `action_*` method that advances a multi-step workflow
    state must open with a `has_group` check** (gotcha #15). Write the check once
    outside the `for rec in self` loop so it fails fast on the first call.
    ```python
    def action_hr_approve(self):
        self._check_loan()
        if not self.env.su and not self.env.user.has_group(
            'KSW_deduction.group_loan_hr'
        ):
            raise UserError(_('Only HR Approvers can approve at the HR step.'))
        for rec in self:
            ...
    ```

27. **When overriding `_action_approve_attendance_based` / `_action_validate` (or
    any method) for a filtered subset of `self`, always process the complement via
    `super()` in the same call.** Anti-pattern:
    ```python
    # WRONG — when attendance_leaves is non-empty, non_attendance is silently dropped
    attendance_leaves = self.filtered('x_attendance_ids')
    if not attendance_leaves:
        return super()._action_validate(...)   # only reached when subset is empty
    for leave in attendance_leaves:
        ...
    ```
    Correct pattern:
    ```python
    attendance_leaves = self.filtered('x_attendance_ids')
    non_attendance = self - attendance_leaves
    if non_attendance:
        super(HrLeave, non_attendance)._action_validate(...)
    for leave in attendance_leaves:
        ...
    ```
    Apply this pattern to every override that filters `self` and handles only a
    portion. Found in `KSW_leave_approval/models/hr_leave.py` for both
    `_action_approve_attendance_based` and `_action_validate`.

28. **Every `create()` override that sets an initial `x_annual_approval_state`
    must also call `_notify_pending_approvers(leave, state)`.** Without it the
    approver receives no inbox notification and the workflow starts silently.
    ```python
    leave.sudo().write({'x_annual_approval_state': 'pending_dm'})
    self._notify_pending_approvers(leave, 'pending_dm')   # ← always pair these
    ```
    Found in `KSW_unpaid_leave/models/hr_leave.py` `create()` — the annual-leave
    `create()` notified correctly; the unpaid override did not. Every new leave
    type that forks the multi-step chain must include the notify call.

29. **Attendance sheet lock/unlock must only lock lines that were already
    `is_attended=True`.** If the lock searches without `('is_attended', '=', True)`,
    it also locks lines that were already absent — and unlock then unconditionally
    restores them to attended, corrupting historical attendance for days the employee
    genuinely missed before the leave was created.
    ```python
    # Correct lock domain in _lock_attendance_sheet_lines:
    lines = self.env['ksw.attendance.sheet.line'].sudo().search([
        ('sheet_id.employee_id', '=', leave.employee_id.id),
        ('date', '>=', date_from),
        ('date', '<=', date_to),
        ('x_leave_id', '=', False),
        ('is_attended', '=', True),   # ← only lock attended lines
    ])
    ```
    With this filter, `_unlock_attendance_sheet_lines` can unconditionally restore
    every locked line to `is_attended=True` without remembering prior state.

30. **When a dependent module redefines `compute=` on fields declared in the parent
    module, the new compute wins for ALL records — do NOT zero-out fields for
    "records that don't apply to this module".** `KSW_eos_leave` redefined
    `x_eos_service_years / x_eos_last_wage / x_eos_termination_amount /
    x_eos_resignation_amount` and zeroed them whenever `x_is_eos_leave` was
    False — which includes every ordinary annual leave, breaking the EOS reference
    panel on the annual leave approval form. Rule: only zero-out when source data
    is genuinely absent (no employee, no joining date, no wage). The dependent
    module should add its *additional* fields (adjusted amounts, payout amounts)
    without blanking the base values for unrelated records.
    ```python
    # WRONG — blanks EOS panel on all ordinary annual leaves
    if not leave.x_is_eos_leave or not leave.employee_id:
        leave.x_eos_service_years = 0.0
        ...
        continue

    # CORRECT — only zero out when data is missing
    if not leave.employee_id:
        leave.x_eos_service_years = 0.0
        ...
        continue
    ```

31. **Model-level `groups=` on a field causes an OWL "field is undefined" crash when
    any view element with a broader (or absent) `groups=` uses that field in an
    `invisible=` expression.** Odoo 19's OWL parser calls `fields_get()` first and
    then resolves every `invisible=` expression against that list. A model-level
    `groups=` gate causes `fields_get()` to omit the field entirely for non-group
    users — even if the view-level `groups=` on the *field element* would hide it.
    The dangerous case is a button with `groups="A,B"` that also has
    `invisible="not some_field"`, while `some_field` has model `groups='A'` only:
    group-B users see the button (it passes the view filter) but `some_field` is
    missing from their `fields_get()` → crash.

    **Rule: Never put `groups=` on a model field if that field is referenced in an
    `invisible=` expression on any view element that could be visible to users
    outside that group.**

    - If the compute uses `sudo()` to access sensitive underlying data, the output
      field does **not** need model-level `groups=`. Use view-level `groups=` only
      to control where it is displayed.
    - If the underlying field value itself is sensitive AND must be model-gated,
      ensure every view element whose `invisible=` references it also has a
      `groups=` that is a strict *subset* of the model field's groups (so the
      element is always filtered out before the client parses it).
    - When adding a button with `groups="A,B"` and an `invisible=` referencing
      `some_field`, verify the model field's `groups=` includes both A and B.

    **Cases fixed July 2026:**
    - `x_gross_salary` on `ksw.deduction` — removed model `groups='hr.group_hr_user'`;
      compute already uses `.sudo()`. View groups expanded to all deduction/loan groups.
    - `x_eos_payslip_id` on `hr.leave` — removed model
      `groups='om_hr_payroll.group_hr_payroll_user'`; the "Recompute EOS Payslip"
      button had `groups="...payroll_user,...leave_hr"` so HR-leave users saw the
      button but couldn't resolve the field. Removing the model gate fixed it.

    **Audit command before adding any `groups=` to a model field:**
    ```bash
    grep -rn "invisible=.*FIELD_NAME" custom_addons/KSW/*/views/*.xml
    # For each hit, verify the containing element's groups= is a strict subset
    # of the model field's groups=
    ```

32. **The MIRROR of #31: a `store=True` custom field added to `hr.employee`
    WITHOUT `groups=` crashes a regular employee's own forms with
    `AccessError: "The fields '…' … are not available for employee public
    profiles."`** A non-HR user has no model-level read on `hr.employee`; the ORM
    serves their own record through the `hr.employee.public` HACK path
    (`hr_employee.py::fetch`/`search_fetch` → `_check_private_fields`). That path
    fetches every stored field returned by `_determine_fields_to_fetch()` **that
    the user is group-allowed to read** and copies it from the `hr.employee.public`
    SQL view — which only exposes a curated field list. A stored custom field with
    no `groups=` is group-allowed for everyone, so it enters the fetch set, is
    absent from the public model, and raises. Because **every other** KSW
    `hr.employee` field carries `groups='hr.group_hr_user'`, such a field is
    usually the *only* one listed in the error (that's the tell).
    **Fix: give the field `groups='hr.group_hr_user'`** (or the appropriate HR/
    payroll group) so non-HR users skip it in the fetch. `group_hr_payroll_user`
    implies `hr.group_hr_user`, so payroll wizards keep access.
    - Symptom: an ordinary employee opening **Time Off → New** (their leave form
      reads their own employee record) hits the dialog; HR/admin never see it.
    - Rule: **never add a bare stored field to `hr.employee` — always attach an HR
      group**, matching the existing fields. This is the inverse trade-off to #31,
      so first confirm the field is NOT referenced in any `invisible=` on a view
      element visible outside that group (audit command above).
    - **Case fixed July 2026:** `x_exclude_from_payroll` on `hr.employee`
      (`KSW_payroll/models/hr_employee.py`) had no `groups=` — the lone bare
      stored employee field — and crashed every employee's Time Off form. Added
      `groups='hr.group_hr_user'`; it is only shown (view-level `groups=`) on the
      admin-only employee payroll page and read by the payslip batch wizard (a
      payroll-group user), both of which retain access.

33. **`x_attendance_ids` means two different things — duration overrides must gate on
    the leave TYPE, not on the m2m being non-empty.** `_auto_link_absence_attendance()`
    fills `x_attendance_ids` on *ordinary* leave types (business trip, sick, umrah)
    when they are validated, purely to mark the covered absences. But every duration /
    display override filtered on `self.filtered('x_attendance_ids')`, so an ordinary
    leave was silently reclassified as an attendance-issue leave and
    `number_of_days` became **the count of linked absence rows** (KSWCO leave 4853:
    Jul 22 → Jul 31 displayed "6 days — 6 records"; the two Fridays produce no absence
    row and Jul 29–31 had not synced yet). The number also crept up nightly, because
    `hr.attendance.create()` auto-links new absences to already-validated leaves.
    Use the gate helper instead:
    ```python
    def _attendance_issue_leaves(self):
        return self.filtered(
            lambda l: l.x_attendance_ids and (
                l.holiday_status_id.is_attendance_issue or l.request_unit_hours
            )
        )
    ```
    `request_unit_hours` is in the gate on purpose — the live Late/Early Excuse types
    are NOT flagged `is_attendance_issue` yet must keep the accepted-minutes duration.
    Related traps fixed in the same pass (`KSW_attendance_leave/models/hr_leave.py`):
    `_get_daily_work_hours(emp)` without a date summed the **whole week** (48.5 h) and
    leaked into `number_of_hours`; counting days off `date_from.date()` is off by one
    in Riyadh (21:00 → 20:59 UTC) so use `request_date_from/to`; and a work-schedule
    group whose lines expired (`end_date` in the past) yields a **0-day** leave —
    `_count_group_line_days` now falls back to calendar days when no group line covers
    any day of the range. When repairing stored durations, **exclude annual leaves** —
    theirs depends on the balance at request time, so recomputing restamps them with
    today's balance.

34. **A non-HR user's employee search never reaches `hr.employee` — it is
    answered by `hr.employee.public`.** `hr.employee.search_fetch()` and
    `hr.employee._search()` both start with
    `if self.browse().has_access('read')` and, when false, delegate the whole
    query to `hr.employee.public` (the "HACK" comment in
    `addons/hr/models/hr_employee.py`). So an override of
    `_search_display_name` / `_name_search` on `hr.employee` alone is dead
    code for exactly the users you wrote it for. **Always override on both
    models** (`hr.employee` *and* `hr.employee.public`); ids match between
    them, so a shared helper returning `Domain('id', 'in', <sudo search>)`
    works for either. See
    `KSW_deduction/models/hr_employee.py::_deduction_identifier_domain` —
    it lets the Accounting Data Entry role find employees by SSN, iqama,
    employee no. and BAS loan account, none of which the stock
    `_rec_names_search` path can match for them (`ssnid` /
    `identification_id` are on `hr.version`, whose record rules expose only
    the user's own version; `x_employee_no` / `x_loan_acc_no` are
    `hr.group_hr_user`-gated).

35. **A new msgid must be added to the module's `.pot`, or its `.po`
    translation is silently discarded on import.** `PoFileReader.__init__`
    (in `odoo/tools/translate.py`) does `self.pofile.merge(polib.pofile(pot_path))`
    when a sibling `<module>.pot` exists — polib's `merge` marks every entry
    absent from the template as **obsolete**, and `__iter__` skips obsolete
    entries. Symptom: `odoo-bin i18n import` and `-u --i18n-overwrite` both
    report success, the `.po` looks perfect, and the DB `jsonb` value keeps
    only `en_US`. Fix: append the same `msgid` block (empty `msgstr`) to
    `i18n/<module>.pot`, then re-run the update. Verify in the DB, never in
    the file:
    ```bash
    psql -d odoo_dev -c "select g.name from res_groups g join ir_model_data d \
      on d.model='res.groups' and d.res_id=g.id where d.name='group_acc_data_entry';"
    ```

36. **A rule evaluated once, from a one-day cron snapshot, is evaluated too
    early.** `cron_generate_absences` decided the Friday weekend grant at 01:00
    the next morning by asking whether the adjacent workday was attended — but
    Thursday's absence was not yet *covered* (the excuse leave gets approved
    days later) and Saturday had not been synced. Grant refused, and since the
    cron window only ever covers *yesterday*, that Friday was never revisited.
    Seven employees permanently lost a paid rest day. The rule was correct
    (`_generate_weekend_records` already counts `x_is_covered` absences as
    attended) — only the timing was wrong. **Whenever a scheduled job reads
    state that keeps arriving after it runs, you need both:** (a) the write path
    that changes the state re-triggers the evaluation —
    `hr.leave._recheck_weekend_grants()` off `_validate_leave_request` /
    `action_refuse` / `action_draft`; and (b) the periodic job keeps the
    decision open — `_WEEKEND_RECHECK_LOOKBACK = 7` days, not just yesterday.
    Both require the pass to be idempotent **in both directions** (skip what
    exists, revoke what is no longer earned). Two traps when wiring the
    re-trigger: `env.flush_all()` first, because `x_is_covered` is a *stored*
    compute the pass reads from the DB; and pass `commit=False`, because the
    pass commits per employee as a cron checkpoint and you are inside an open
    HTTP transaction.

37. **A finalised record has more than one way out — guarding the button you
    were shown is not locking it.** Locking approved time off (Aug 2026) meant
    closing **six** routes, not one: `action_refuse`, `action_draft`,
    `_move_validate_leave_to_confirm` ("Back to Approval"), the Cancel wizard
    (`_action_user_cancel` → `_force_cancel`), `unlink`
    (`_unlink_if_correct_states`), and a raw `write({'state': 'refuse'})` over
    RPC — core's `_check_approval_update` lets the employee's own
    `leave_manager_id` do that last one on a *validated* leave. Two subtleties:
    `_force_cancel` writes the state through `sudo()`, so a `write()` guard
    that exempts `env.su` (as it must, or crons break) never sees the Cancel
    wizard — it needs its own override; and the KSW chains keep
    `state == 'confirm'` all the way past GM final approval, so any
    `state`-based check waves a fully-approved request straight through.
    Pattern: **one predicate, one guard, called from every route** —
    `hr.leave._is_finalised()` (extended by KSW_payroll / KSW_eos_leave to
    cover a `done` payslip) + `_check_final_reversal_rights(what)` gating on
    `base.group_system`, with `env.su` exempt. Button-visibility computes
    (`can_refuse`, `can_cancel`, `can_back_to_approve`) follow the guard and
    need `@api.depends_context('uid')` (gotcha #14). Audit command before
    declaring a state locked:
    ```bash
    grep -rn "def action_.*\(refuse\|cancel\|draft\|reject\|reset\|reopen\)" custom_addons/KSW --include=*.py
    grep -rn "_unlink_if_correct_states\|_force_cancel\|write({'state'" addons/<core_module>
    ```

38. **Authority in Python and scope in `ir.rule` are different systems — a guard
    that names a group is worthless if that group cannot reach the records.**
    `KSW_annual_leave/security/security.xml` deliberately neutralises Odoo's own
    `hr.leave` rules (the stock *Time Off Administrator* rule is rewritten to
    `[(0, '=', 1)]`) so the KSW tiers own 100% of the CRUD scope, and it
    compensated by writing `group_leave_officer` onto **`base.user_root` and
    `base.user_admin` via `user_ids`** — the two built-in accounts only. So
    `base.group_system`, made the sole holder of the leave-reversal rights in
    Aug 2026, had no `hr.leave` access whatsoever; `admin` only worked because
    it happens to be one of those two accounts. Before making a group the sole
    holder of an action, verify it can reach the model — and note an
    `ir.model.access` row reading `1,1,1,1` proves nothing when an `ir.rule`
    is neutralised:
    ```bash
    psql -d odoo_dev -c "select r.name, r.domain_force, r.perm_unlink from ir_rule r \
      join ir_model m on m.id=r.model_id where m.model='hr.leave' and r.active;"
    ```
    Fix with an **implication**, not a per-user patch: `user_ids` covers the
    accounts that exist today, `implied_ids` covers the role
    (`base.group_system` now implies `group_leave_officer`). Same pass: a wizard
    opened to a new audience needs its **own `ir.model.access` row** (record
    rules are not the only gate), and any flow that posts to the chatter needs
    `sudo()` after the auth check because `mail.message` create is gated on
    access to the document (gotcha #11).

39. **A dynamic `selection=` method cannot depend on the record — the web
    client strips the context and caches the payload.**
    `view_service.js::loadViews` filters the context down to `lang` and
    `*_view_ref` before calling `get_views`, then caches the result on disk
    (IndexedDB) per model. So `fields.Selection(selection='_method')` reading
    `default_x_id` from `self.env.context` always takes its no-context branch,
    and even if a key survived, the second dialog in a session would reuse the
    first one's options. **This is invisible from `odoo shell`**: calling
    `Model.with_context(default_x_id=1).get_views(...)` by hand returns a
    perfectly filtered list and "proves" a feature that is broken in the
    browser — when verifying view-layer behaviour from the shell, strip the
    context exactly as the client does.
    **Fix: make it relational.** `radio_field.js` reads
    `record.fields[name].selection` for a Selection (cached), but calls
    `getFieldDomain(props.record, ...)` for a Many2one — resolved against the
    record, every time:
    ```python
    allowed_step_ids = fields.Many2many('ksw.leave.return.step', compute='...')
    target_step_id = fields.Many2one(
        'ksw.leave.return.step', required=True,
        domain="[('id', 'in', allowed_step_ids)]")
    ```
    Costs a small reference model + data file; per-record correct. Options that
    vary per *record* need a relational field with a domain; a dynamic
    `selection=` may only vary by what survives into `get_views` (user, groups,
    `lang`, installed modules). The client's disk cache is namespaced by
    `session.registry_hash`, which changes on `-u`, so a page reload suffices
    after an upgrade — but a tab left open across it keeps the old view.

40. **A wizard compute that depends on a field needs that field in the form
    arch — `default_x_id` reaching `default_get` is not the same as reaching
    the client's record.** The client builds its record from the fields the
    *view* asks for. `ksw.gm.return.approver.wizard` never listed `leave_id`
    in its arch: harmless for a year, because `action_confirm` reads
    `self.leave_id` off the **saved** record where `default_get` does apply the
    default — but the moment a *computed* field depended on it at render time,
    the dialog went blank. The client's onchange simply omitted `leave_id`, so
    the compute ran against an empty leave and returned nothing. Fix:
    `<field name="leave_id" invisible="1"/>`.
    **Every shell probe passed** (`create({'leave_id': ...})`,
    `new(default_get(...))`, `onchange(...)` with an explicit spec) because
    they all supplied `leave_id` themselves. To verify a dialog, drive it:
    `HttpCase.browser_js` runs real Chrome, but needs `websocket-client`, which
    lives **only in the project `.venv`** — with `odoo19env` the test *silently
    skips* and prints "0 failed":
    ```bash
    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 --test-enable \
      --test-tags /KSW_annual_leave:TestReturnWizardUI -u KSW_annual_leave --stop-after-init
    ```
    The run's CDP log contains the client's exact RPC payloads — that is where
    the missing `leave_id` was visible. Pattern in
    `KSW_annual_leave/tests/test_return_wizard_ui.py`.

41. **A menu's effective `groups=` is the INTERSECTION of its own and every
    ancestor's — a child under a narrower parent is orphaned.**
    `ir_ui_menu._visible_menu_ids` walks a menu's ancestors into the visible
    set with `while menu_id not in visible_ids and menu_id in menu_ids`, where
    `menu_ids` is already filtered by the user's groups. A parent that failed
    that filter is not in `menu_ids`, the walk stops, and the child — still in
    `visible_ids` — has no visible parent to render under. Hit by
    `menu_ksw_pay_recurring`, which was `groups=group_commission_officer` under
    a Configuration parent gated to `group_commission_admin`: **an Officer who
    was not an Admin never saw it**, with nothing in the data or logs to say
    so. Symptom: "role X can't find the menu", while the ACL, the record rules
    and the item's own `groups=` all name role X. Before putting an item under
    an existing section, walk `parent=` upward and check every `groups=` on the
    way. Related to gotcha #1 (menuitem overrides) and #38 (a group in a guard
    with no rule to reach the records).

42. **Inside a `compute_sudo` compute, `env.su` is not evidence of privilege —
    and neither is any helper you call from it.** The module already knew to
    read authority from the user's *group* rather than `env.su` in such a
    compute (gotcha in `ksw.pay.batch._allowed_employees`). The part that bites
    is one level down: `ksw.pay.batch._allowed_departments()` opens with
    `if self.env.su or user.has_group(officer): return <every department>`, so
    calling it from a `compute_sudo` field handed every supervisor all ~400
    employees — inverting the very picker the field exists to narrow.
    ```python
    # WRONG inside a compute_sudo compute — env.su is true for everybody
    departments = self.env['ksw.pay.batch']._allowed_departments()
    # RIGHT — ask the authority question as the user; the reads inside are
    # already sudo()'d, so nothing is lost
    Batch = self.env['ksw.pay.batch'].with_env(self.env(su=False))
    departments = Batch._allowed_departments()
    ```
    `ksw.pay.batch` never hit it because its own `_allowed_employees` branches
    on `self.department_id` first and only reaches the helper in the fallback
    branch — **the trap was dormant, not absent**. When reusing a scope helper
    in a new context, check what it does with `env.su`, not just what it
    returns. And assert the *negative* in the test (`assertNotIn(outsider, …)`)
    — the positive half passed throughout.

43. **A client is a *role on `res.partner`*, never a second table and never an
    app-owned checkbox.** SAP S/4HANA (one Business Partner + additive **BP
    roles** `FLCU00`/`FLVN00`/`BUP003`), Oracle Fusion TCA (one `HZ_PARTIES`
    row + **party usages**), Dynamics 365 (`DirPartyTable` + `CustTable`/
    `VendTable`/`HcmWorker`) and Odoo all converge on **one party master, many
    roles** — because a client can also be a vendor and an employee can also be
    a customer. Odoo deleted the `customer`/`supplier` booleans in v13 for
    exactly this reason. **Every KSW partner picker uses Odoo's native marker:**
    ```python
    # model — single source of truth
    client_id = fields.Many2one(
        'res.partner', domain="[('customer_rank', '>', 0)]")   # vendors: supplier_rank
    ```
    ```xml
    <!-- view — a STATIC context dict; a dynamic context on the field
         definition crashes web_read during onchange -->
    <field name="client_id" context="{'res_partner_search_mode': 'customer'}"/>
    ```
    `res_partner_search_mode` both orders the picker by rank **and** stamps
    `customer_rank = 1` on anything quick-created from it
    (`addons/account/models/partner.py:356-363` and `:769-780`), so "the client
    isn't in the list" needs no wizard. Never add
    `x_is_<app>_client` — Accounting, Supply Chain and Production would each
    repeat it. Three traps: `customer_rank` lives in **`account`**, so a module
    depending only on `hr` must add it; a field defaulting to
    `env.company.partner_id` falls **outside its own domain** unless that
    partner is stamped (`KSW_fleet._post_init_hook` does this for every
    `res.company`, not just `env.company`); and a Many2one `domain=` is a UI
    hint, not an ORM constraint — test the **domain and what it selects**, not
    a write that fails. Full writeup: Odoo 19 Pitfalls #101.

44. **"Covered by Time Off" answers two questions at once — so an unpaid leave
    paid the employee in full.** `_auto_link_absence_attendance()` links the
    absence rows of *any* validated non-attendance-issue leave to that leave,
    and the link sets `hr.attendance.x_is_covered`, which payroll reads as
    **excused *and paid***: `x_net_is_absent` clears, a full scheduled day is
    credited to `x_net_worked_hours`, `x_deduction_amount` goes to 0, and
    `_worked_day_lines_biometric` counts the day in **WORK100** instead of
    `ATT_ABS`. Unpaid leave travelled that path unchanged (KSWCO payslip 18099
    / leave 4838: WORK100 24 days, ATT_ABS 7 — the whole point of the leave
    inverted), and the Fridays inside the block were granted on top, because
    `_generate_weekend_records` counts a *covered* absence as an attended
    adjacent workday (#36).
    **The link is right; only the payment half was wrong.** Attaching the
    absence says *why the day is empty*; that is not the same question as
    *is he paid for it*. Split them with a hook — `is_unpaid_leave` lives on
    `hr.leave.type` in KSW_unpaid_leave, which **depends on**
    KSW_attendance_leave, so the compute cannot name the flag:
    ```python
    # KSW_attendance_leave/models/hr_leave.py — every type pays by default
    def _excuses_absence(self):
        return self
    # KSW_unpaid_leave/models/hr_leave.py — this one does not
    def _excuses_absence(self):
        return super()._excuses_absence().filtered(
            lambda l: not self._is_unpaid_leave(l))
    ```
    `_compute_is_covered` / `_compute_net_minutes` run the validated leaves
    through it; `x_attendance_ids` stays populated either way. Two follow-ons:
    `x_is_covered` is a **stored** compute, so old rows keep the wrong value
    until something recomputes them (`KSW_unpaid_leave/migrations/19.0.1.2.0`
    re-runs `_recompute_deductions()` + `_regenerate_weekends_for_leaves()`),
    and a `done` payslip is a paid document — repairing one is a Payslip
    Revision, not a data fix. Same pass: `_validate_leave_request` gated on
    `self.filtered('x_attendance_ids')` instead of `_attendance_issue_leaves()`
    (#33), so an ordinary leave validated a *second* time skipped `super()`.

45. **A `post-migrate` runs right after its OWN module is loaded, so a field
    declared by a module further down the dependency chain is not in the
    registry — `modified()` notifies nothing and raises nothing.** The
    KSW_unpaid_leave `19.0.1.2.0` post-migrate un-covered 38 KSWCO attendance
    rows and called `_recompute_deductions()`; the flags came out right and
    `x_deduction_amount` stayed **0** on every one of them, because that field
    belongs to `KSW_payroll`, which loads later in the same run
    (`Loading module KSW_unpaid_leave (105/110)` … `KSW_payroll` after it).
    Half a repair reads exactly like a whole one until you check the value the
    consumer reads rather than the flag you set.
    **Use `end-migrate` for anything touching a lower module** — it runs after
    *every* module is loaded (`odoo/modules/loading.py`, "STEP 3.5: execute
    migration end-scripts"). Ask "which module declares this field?" before
    choosing `pre`/`post`/`end`, and guard it anyway:
    ```python
    if 'x_deduction_amount' not in env['hr.attendance']._fields:
        _logger.info('KSW_payroll not loaded yet — end-migrate finishes this.')
    ```
    Migrations fire only on a **version change**, so finishing a job that
    already ran needs a new version folder (`19.0.1.2.1/end-migrate.py`); write
    the pass idempotent and running both costs nothing.

46. **`x_return_state` is the return-confirmation gate for ANY leave type, not
    just annual — never re-add a `holiday_status_id.is_annual_leave` clause
    next to it.** `hr.payslip._get_unresolved_vacation_leaves` is read by three
    places at once (the `compute_sheet` guard, the batch skip in
    `hr.payslip.employees._check_employee_for_batch`, and the attendance
    sheet's `_confirmation_blockers`), and its type clause made all three blind
    to unpaid leave: an employee was paid for a month nobody had confirmed he
    came back from. The field only ever leaves `'not_applicable'` on a type
    that uses the system, so **the state is the filter** — that is also why the
    punch alert (`KSW_annual_leave/models/hr_attendance.py`) and
    `attendance_sheet._leave_coverage_end` no longer name a type.
    Unpaid leave (Sep 2026) stamps `on_vacation` in `_action_validate` and
    calls `_notify_return_confirmation_due()` — one message to the DM at
    approval, never a recurring chase.
    **The two flows differ on exactly one thing: what shortening means.** An
    annual vacation is paid up front, so `_shorten_to_confirmed_return`
    deliberately preserves the duration and only moves the covered period; an
    unpaid leave is deducted in arrears, so its duration **must** shrink —
    every trimmed day is a day paid again. And
    `KSW_unpaid_leave._apply_confirmed_return` must **never** call
    `_sync_opening_reset_to_return`: that restarts the annual accrual from zero
    on the return date, which is right for a vacation and would wipe an
    employee's whole balance for taking leave that paid them nothing. A type
    flagged both annual and unpaid keeps the annual path —
    `_uses_unpaid_return()` is the switch.

47. **View inheritance may not use a translated attribute as an xpath
    selector.** `//widget[@name='web_ribbon'][@title='🏖️ On Vacation']` raises
    `ValidationError: View inheritance may not use attribute 'title' as a
    selector` — `ir_ui_view._valid_inheritance` rejects `TRANSLATED_ATTRS`
    (`title`, `string`, `help`, `placeholder`, `confirm`…) because the selector
    would stop matching in Arabic and fail silently in that language only.
    Give the target a non-translated handle in the view that owns it
    (`class="o_ksw_ribbon_on_vacation"`) and select with `hasclass()` — not
    `[@class='…']`, which Odoo warns about separately since a class list is not
    an identity. Comment it as a selector handle or the next reader deletes it
    as dead CSS.

48. **A status marker belongs where the real-world event happens, not where the
    record finalises — and `x_annual_approval_state` outlives the request.**
    `x_return_state = 'on_vacation'` was stamped in `_action_validate`, i.e. at
    **Step 6** when HR files the signed form. The employee leaves at **Step 5**,
    when the GM signs, and the KSW chain sits in `state == 'confirm'` across the
    whole gap — so for days nothing flagged the employee as away: no ribbon, no
    punch alert, and `attendance_sheet._leave_coverage_end` saw an ordinary
    month. The two moments coincide on stock Odoo flows and never on a
    multi-step chain. Two traps when fixing it:
    ```python
    def _is_past_gm_final(self):
        if self.state in ('refuse', 'cancel', 'draft'):
            return False   # a CANCELLED request still reads 'approved'
        if self._uses_multi_step_chain(self):
            return self.x_annual_approval_state in self._REFUSE_LOCKED_STATES
        return self.state == 'validate'
    ```
    and: the forward stamp needs the same one-predicate-called-from-every-route
    treatment as the backward guard (#37). Seven routes touch this state (GM
    final, validate, refuse, draft, back to approval, the Cancel wizard, the GM
    return wizard) — `_sync_gm_final_state()` re-derives it from all seven, so
    "returning *to* Step 6 keeps the marker, every earlier target clears it"
    falls out instead of being a seventh rule. When a sibling module has a side
    effect at the same moment, hang it on the same hook: `KSW_eos_leave`
    overrides `_sync_gm_final_state()` for the **employee archive** (which also
    began at GM final and was never undone), guarded on a recorded
    `x_eos_employee_archived` — never on `not employee.active`, or refusing a
    stale draft resurrects someone HR archived for their own reasons. Order at
    the set point: `_create_vacation_payslip()` runs **before** the stamp,
    because `x_return_state` is the payslip guard.

49. **"Confirm" on a batch/action method must be idempotent — never assume
    every record passed in is still in the state the method expects.**
    `hr.payslip.run.done_payslip_run()` looped over `self.slip_ids`
    unconditionally: `for line in self.slip_ids: line.action_payslip_done()`.
    Reopening a *closed* batch back to `draft` (`draft_payslip_run`, Payroll
    Manager only — e.g. to add one late employee) does **not** touch the
    individual slips' own state; they stay `done`. Re-clicking "Mark As
    Done" then swept every already-`done` slip through
    `action_payslip_done()` again, which (base `om_hr_payroll`) always calls
    `compute_sheet()` first. KSW_deduction's `compute_sheet()` override
    unconditionally deletes+re-searches `KSW_DED_*` inputs, but only for
    `state='pending'` installments — an installment already settled
    (`state='paid'`, `payslip_id` set) doesn't match, so it silently
    vanished from the payslip's Other Inputs and the recomputed NET, while
    `ksw.deduction.line` kept correctly saying it was collected under that
    exact payslip. Symptom: payslip shows no loan/deduction, but the
    deduction record says the installment was settled by that very payslip
    — the two disagreeing is the tell, not an error anywhere. Hit KSWCO
    payslip 18441 (August 2026 batch) this way; confirmed via a full-batch
    reconciliation query that only 1 of 376 slips was affected (the batch
    hadn't been mass-reopened, just this one late addition).
    **Fix, three independent layers (defense in depth — any one alone
    would have prevented this incident, keep all three):**
    ```python
    # 1) KSW_payroll/models/hr_payslip_run.py — only confirm slips still
    #    draft; an already-done slip inside a reopened batch is skipped.
    def done_payslip_run(self):
        pending = self.slip_ids.filtered(lambda s: s.state != 'done')
        for line in pending:
            line.with_context(_ksw_skip_bank_refresh=True).action_payslip_done()
        ...

    # 2) KSW_payroll/models/hr_payslip.py — action_payslip_done() is a
    #    no-op on an already-done slip, from ANY caller, not just the batch.
    payable = (self - overpaid).filtered(lambda s: s.state != 'done')
    if not payable:
        return notification

    # 3) KSW_deduction/models/hr_payslip.py — the real backstop: even if
    #    compute_sheet() DOES get called on a done payslip (any future
    #    caller, not just these two), its KSW_DED_* inputs must never move.
    live = self.filtered(lambda s: s.state != 'done')
    for payslip in live:
        self._inject_ksw_deduction_inputs(payslip)   # skipped when done
    res = super().compute_sheet()   # still runs for ALL self — rebuilds
    # other salary lines from the (untouched) existing inputs, so a done
    # payslip's NET keeps reflecting a settlement it already made.
    ```
    Layer 3 is the one that actually matters long-term: layers 1–2 close
    the two call paths found this time, but the next unknown caller of
    `compute_sheet()` on a `done` payslip is neutralized by layer 3 alone.
    **Test pattern** (`KSW_deduction/tests/test_critical_fixes.py::
    TestPayrollPriority.test_done_payslip_recompute_preserves_settled_deduction`):
    settle a line by a **raw** `write({'state': 'done'})` rather than
    through `action_payslip_done()`'s own capping pass — this class's
    fully-attended-sheet fixture currently can't produce an affordable NET
    (pre-existing, unrelated staleness: ATTDED comes out as the whole
    wage), so routing settlement through the real capping logic makes the
    test fail for the wrong reason. Bypassing it isolates exactly the
    property being guarded: does a second `compute_sheet()` /
    `action_payslip_done()` on an already-`done` payslip leave a settled
    installment alone. Verified via failing-set diff (gotcha-standard
    practice): identical 14/57 pre-existing KSW_deduction/KSW_payroll
    failures with or without the fix — this test is the only one whose
    outcome flips (fails on baseline, passes fixed) — before touching
    production. **Data repair for an already-corrupted payslip** (18441):
    do **not** re-run `compute_sheet()` blindly — it won't re-inject a
    `paid` installment either. Instead: temporarily reopen the installment
    (`state='pending'`, clear `payslip_id`), run `compute_sheet()` (now
    picks it up correctly and rebuilds NET), then re-settle via the
    module's own `_sync_deductions_on_done(slip)` — reuses real ORM logic
    instead of hand-writing `hr.payslip.line` rows (many required fields,
    hidden invariants). Confirm which NET the bank actually paid **before**
    touching any figure — this shop marks a payslip `done` only *after*
    issuing the transfer, so `done` implies the stored NET matches what
    left the bank; that ordering is what makes the repair safe rather than
    a second, silent discrepancy.
