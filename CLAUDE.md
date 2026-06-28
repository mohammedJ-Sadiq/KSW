# KSW Odoo Project — Claude Context

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
| `KSW_annual_leave` | Saudi-law annual leave: 21 days/yr (<5 yrs), 30 days/yr (≥5 yrs), auto-allocated daily |
| `KSW_unpaid_leave` | Unpaid leave with 2-step approval + attendance integration |
| `KSW_leave_approval` | 2-step time-off approval: Direct Manager → HR Manager |
| `KSW_deduction` | Employee deductions (loans, penalties, advances) |
| `KSW_commissions` | Monthly commissions & allowances for non-biometric employees |
| `KSW_payroll` | Full payroll: extends `om_hr_payroll`, biometric attendance deduction, payslip runs, bank export |

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

## KSW_deduction architecture notes

### managed_by field
`ksw.deduction.type.managed_by` (`Selection`: `'hr'` / `'accounting'`, default `'hr'`)
controls which department can manually close installments outside payroll.

| Type | managed_by |
|------|-----------|
| Loan | `accounting` |
| Salary Advance | `hr` |
| Gov. Penalty | `hr` |
| Internal Penalty | `hr` |

`ksw.deduction.managed_by` is a `store=True` related field that mirrors the type's
value. Stored so record rules can filter on it without a subquery.

### Installment privilege matrix

| Group | HR-managed deductions | Accounting-managed (loans) |
|-------|----------------------|---------------------------|
| `group_deduction_officer` | can mark paid, edit schedule | ✗ |
| `group_loan_hr` | can mark paid, edit schedule | see loans only (via record rule) |
| `group_installment_edit` | can mark paid, edit schedule | can mark paid, edit schedule, open wizard |

`x_can_edit_installments` is a non-stored `@api.depends_context('uid')` computed
Boolean on `ksw.deduction` that reflects this matrix for the current user. The view
uses it for `invisible=` on the installments tab; Python `create()`/`write()` guards
enforce it at the ORM level.

### action_mark_line_paid
`ksw.deduction.line.action_mark_line_paid()` stamps a single pending line as paid
in-place (`state='paid'`, `is_manual=True`, `manual_by`, `manual_date`). Posts a
chatter note on the parent deduction. Auto-completes the deduction if all lines are
now paid. Uses `ded.sudo()` for both `message_post` and `write({'state': 'completed'})`
because the accounting user may lack write scope to HR-managed deductions via record
rules, yet is authorised to trigger auto-completion (auth is checked before the loop).

### ksw.loan.payment.wizard
`wizard/ksw_loan_payment_wizard.py` — TransientModel for partial or full loan
payments outside payroll. Only `group_installment_edit` users can open it (ACL +
Python auth guard inside `action_confirm`).

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

### Record rule: group_loan_hr sees HR-managed deductions
`ksw_deduction_rule_hr_managed` (in `KSW_deduction/security/security.xml`) ORs with
the existing loan-approvers rule so `group_loan_hr` users see:
loans (`is_loan=True`) **OR** HR-managed deductions (`managed_by='hr'`).
The `managed_by` field must be `store=True` on `ksw.deduction` for this domain to work.
