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
node build_pdf.mjs        # both languages -> ../pdf/KSW-User-Manual-EN.pdf, -AR.pdf
node build_pdf.mjs en     # one language
```

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
