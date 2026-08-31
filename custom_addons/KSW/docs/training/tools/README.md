# Screenshot Tooling

Automation that captures the training screenshots from the **development**
system (`localhost:8070` / `odoo_dev`) — never production.

## Files

| File | Purpose |
|---|---|
| `setup_demo_data.py` | Creates the per-persona `train.*` login users (+ demo employees and a sample loan) in `odoo_dev`. Run via Odoo shell. Idempotent. |
| `stage_records.py` | Stages loans in each approval state + a commission sheet; writes ids to `_demo_ids.json`. |
| `stage_leaves.py` | Stages annual leaves in each approval state (+ a validated allocation); merges ids into `_demo_ids.json`. |
| `stage_more.py` | Flags the demo employee as attendance-sheet based + creates an HR salary advance with installments. |
| `stage_helpdesk.py` | Creates the `train.it` login (IT Team), a small IT asset fleet (assigned / available / in maintenance / warranty expiring / expired) and tickets in every stage — including an overdue, a blocked and four closed ones so Reporting and History are not empty. Merges ids into `_demo_ids.json`. |
| `demo_lang_switch.py` | Rewrites the staged helpdesk ticket subjects/bodies (plain data, not translatable fields) between English and Arabic so each language's screenshots show that language. `KSW_DEMO_LANG=ar\|en`. |
| `capture_screenshots.mjs` | Landing-screen shots (lists/dashboards) per persona — navigates by action XML id. |
| `capture_deep.mjs` | Interaction shots (wizards, dialogs, forms in a specific approval state) — reads record ids from `_demo_ids.json`. |
| `build_pdf.mjs` | Builds printable PDF handbooks from the Markdown (Markdown → HTML → headless-Chrome PDF). |
| `package.json` | Node deps (`playwright`, `marked`). |

## Building the PDF handbooks

```bash
cd custom_addons/KSW/docs/training/tools
node build_pdf.mjs             # per-role handbooks, both languages -> ../pdf/
node build_pdf.mjs en          # one language
node build_pdf.mjs full        # one comprehensive all-roles manual
node build_pdf.mjs commission  # commission-only, per role -> ../pdf/commission/
```

`commission` is a **bundle**: it gathers the commission pages out of the role
folders into their own set of PDFs (`COMMISSION_DOCS` / `COMMISSION_ORDER` in
`build_pdf.mjs`), for handing to someone trained on the commission app alone.
`renderDoc` takes an `outDir` so a bundle can write outside `pdf/` without
disturbing the per-role handbooks. To add another bundle, copy that pattern —
a doc map, an order, and one `buildX(lang)`.

Uses `google-chrome-stable` (RTL-aware, embeds screenshots; Arabic uses the
Noto Naskh Arabic font). Screenshots not yet captured render as a labelled
"image pending capture" placeholder, so the PDF is always complete.

## Full setup + capture order

```bash
# users, then staged records, then leaves, then extras (all via odoo shell)
for f in setup_demo_data stage_records stage_leaves stage_more stage_helpdesk; do
  /home/odoo/odoo19env/bin/python3.12 odoo-bin shell -c KSW_dev.conf --no-http \
    < custom_addons/KSW/docs/training/tools/$f.py
done
# then capture (from tools/)
node capture_screenshots.mjs   # 23 landing shots
node capture_deep.mjs          # interaction shots
```

## One-time setup

```bash
# 1. Browser for Playwright (npm, not pip)
cd custom_addons/KSW/docs/training/tools
npm install
npx playwright install chromium

# 2. Create the demo login users in the DEV database
cd /home/odoo/Odoo/odoo
/home/odoo/odoo19env/bin/python3.12 odoo-bin shell -c KSW_dev.conf --no-http \
    < custom_addons/KSW/docs/training/tools/setup_demo_data.py
```

The demo logins are `train.employee`, `train.supervisor`, `train.hr`,
`train.accounting`, `train.gm`, `train.payroll`, `train.it`, `train.admin`, all with the
dev-only password set at the top of `setup_demo_data.py`. They live in the dev
DB only and are not committed. To remove them, unlink `res.users` with
`login like 'train.%'` and the `Train %` employees.

## Capturing

```bash
cd custom_addons/KSW/docs/training/tools
node capture_screenshots.mjs            # all personas
node capture_screenshots.mjs employee    # one persona
```

Navigation is by **action XML id** (`/odoo/action-<xmlid>`), which is stable
across upgrades. Each shot is best-effort — a failure is logged and the run
continues. Re-running overwrites existing PNGs. Regenerate after any UI change
that affects a documented screen.

## ⚠ Pending capture — the rebuilt commission app (Aug 2026)

The commission guides were rewritten for the ERP-element rebuild
(`ksw.pay.batch` / `ksw.pay.recurring` / `ksw.pay.run`). Their screenshots are
**not captured yet** — they render as labelled "image pending capture" boxes in
the PDFs, which build fine. The old commission shots referenced a dead action
(`action_ksw_commission_sheet_my`) and are no longer used.

To capture, stage a month in `odoo_dev` (a draft batch with rows, a recurring
entry, a submitted department, an approved run) and shoot these paths in both
`screenshots/` and `screenshots-ar/`:

| Path | Screen |
|---|---|
| `supervisor/pay-01` | Pay Entries → My Batches (list) |
| `supervisor/pay-02` | New batch header (component / period / department) |
| `supervisor/pay-03` | Entries tab with rows |
| `supervisor/pay-04` | The "how this amount was worked out" dialog |
| `supervisor/recurring-01` | Recurring Entries list |
| `supervisor/recurring-02` | Adding a recurring line |
| `supervisor/recurring-03` | **Add Recurring** on a batch |
| `supervisor/payrun-01` | Monthly Pay Run → Who Gets Paid |
| `supervisor/payrun-02` | **Submit My Entries** |
| `gm/comm-01` … `gm/comm-04` | By Department, Who Gets Paid, Approve My Departments, Return for Correction |
| `accounting/comm-01` … `comm-03` | Approved run, Payment Register, Export Bank File |
| `admin/comp-01` | Pay Component form |

## Coverage status — 63 / 63 + 32 helpdesk captured ✅

All referenced screenshots are captured:
- **~23 landing shots** (`capture_screenshots.mjs`) — every persona's key list/
  dashboard/wizard.
- **~40 interaction shots** (`capture_deep.mjs`) — loan and leave approval-state
  forms (approve buttons, disbursement, wizards), commission sheets (draft +
  confirmed), attendance sheet with day rows, payslip salary lines, payslip
  batch with per-bank totals + **Skipped Employees** section + bank-export
  wizard, sales-commission override, preferences/inbox/waiting-for-me.

- **32 helpdesk shots** (16 per language) — `it/` queue, ticket form, closing,
  filters, asset register + form, assign/return wizards, assignments,
  maintenance, reporting, history, three configuration lists; plus
  `employee/ticket-0{1..4}` and `supervisor/ticket-0{1,2}`.

Data staging is split across idempotent shell scripts, run in order:
`setup_demo_data.py` → `stage_records.py` → `stage_leaves.py` → `stage_more.py`
→ `stage_final.py` → `stage_helpdesk.py`. The payslip/batch shots reuse **real existing records**
(read-only) opened via the payroll persona; their ids are in `_demo_ids.json`.

To refresh everything: run the staging scripts, then `node capture_screenshots.mjs`
and `node capture_deep.mjs`, then `node build_pdf.mjs`.

## Which language a capture run produces

The UI language comes from the **`train.*` users' own `lang`**, not from the
script — `capture_screenshots_ar.mjs` / `capture_deep_ar.mjs` are the same
scripts writing to `screenshots-ar/`. So switch the users before each pass:

```sql
-- English pass
UPDATE res_partner p SET lang='en_US' FROM res_users u
  WHERE u.partner_id=p.id AND u.login LIKE 'train.%';
-- Arabic pass
UPDATE res_partner p SET lang='ar_001' FROM res_users u
  WHERE u.partner_id=p.id AND u.login LIKE 'train.%';
```

Demo **record content** (ticket subjects and bodies) is plain data, not a
translatable field, so it does not follow the UI language. Flip it with:

```bash
KSW_DEMO_LANG=ar .venv/bin/python odoo-bin shell -c KSW_dev.conf \
    < custom_addons/KSW/docs/training/tools/demo_lang_switch.py
```
