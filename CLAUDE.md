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
