/*
 * Capture KSW training screenshots from the DEV system (localhost:8070 / odoo_dev).
 *
 * Prereqs:
 *   npx playwright install chromium        # one-time browser download
 *   node .../tools/setup_demo_data.py       # via odoo shell (creates train.* users)
 *
 * Run:
 *   node capture_screenshots.mjs            # all personas
 *   node capture_screenshots.mjs employee   # one persona
 *
 * Navigation is by action XML id (/odoo/action-<xmlid>) which is stable across
 * upgrades. Each shot is best-effort: a failure is logged and the run continues.
 * Output goes to ../screenshots/<persona>/<name>.png using the naming convention
 * referenced by the guides.
 *
 * NOTE: this captures the reliably-reachable "landing" screens (lists,
 * dashboards, wizards) that show the role-gated UI. Deeper shots that need a
 * record in a specific approval state or a specific form tab open are marked
 * TODO below and are a later, interactive pass.
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { mkdirSync } from 'fs';

const BASE = process.env.KSW_URL || 'http://localhost:8070';
const PASSWORD = process.env.KSW_PW || 'trainKSW#2026';
const __dirname = dirname(fileURLToPath(import.meta.url));
const SHOTDIR = join(__dirname, '..', 'screenshots');

// action XML id per shot; `common` shots are handled specially.
const SHOTS = {
  employee: [
    ['timeoff-01',    'hr_holidays.hr_leave_action_my'],
    ['loan-01',       'KSW_deduction.action_ksw_my_loans'],
    ['payslip-01',    'om_hr_payroll.action_view_hr_payslip_form'],
    ['commission-01', 'KSW_commissions.action_ksw_commission_sheet_my'],
  ],
  supervisor: [
    ['leave-approve-01', 'hr_holidays.hr_leave_action_action_approve_department'],
    ['loan-approve-01',  'KSW_deduction.action_ksw_deduction_loans'],
    ['attsheet-01',      'KSW_attendance_sheet.action_ksw_attendance_sheet'],
    ['comm-01',          'KSW_commissions.action_ksw_commission_sheet_my'],
  ],
  hr: [
    ['leave-hr-01',        'hr_holidays.hr_leave_action_action_approve_department'],
    ['loan-hr-01',         'KSW_deduction.action_ksw_deduction_loans'],
    ['deduction-create-01','KSW_deduction.action_ksw_deduction_hr_managed'],
    ['attsheet-01',        'KSW_attendance_sheet.action_ksw_attendance_sheet'],
  ],
  accounting: [
    ['leave-acc-01', 'hr_holidays.hr_leave_action_action_approve_department'],
    ['loan-acc-01',  'KSW_deduction.action_ksw_deduction_loans'],
    ['comm-01',      'KSW_commissions.action_ksw_commission_sheet_all'],
  ],
  gm: [
    ['leave-gm-01', 'hr_holidays.hr_leave_action_action_approve_department'],
    ['loan-gm-01',  'KSW_deduction.action_ksw_deduction_loans'],
  ],
  payroll: [
    ['batch-01', 'om_hr_payroll.action_hr_payslip_run_tree'],
  ],
  admin: [
    ['ded-dashboard-01', 'KSW_deduction.action_ksw_deduction_dashboard'],
    ['comm-config-01',   'KSW_commissions.action_ksw_commission_category'],
  ],
};

async function login(page, persona) {
  await page.goto(`${BASE}/web/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="login"]', `train.${persona}`);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForSelector('.o_main_navbar', { timeout: 30000 });
}

async function shoot(page, persona, name, action) {
  const dir = join(SHOTDIR, persona);
  mkdirSync(dir, { recursive: true });
  await page.goto(`${BASE}/odoo/action-${action}`, { waitUntil: 'domcontentloaded' });
  // wait for any main view to render
  await page.waitForSelector(
    '.o_list_view, .o_kanban_view, .o_form_view, .o_calendar_view, .o_dashboard',
    { timeout: 20000 });
  await page.waitForTimeout(1200); // let data/labels settle
  const err = await detectError(page);
  if (err) { console.log(`  ⚠ ERROR ON ${persona}/${name}: ${err}`); errs.push(`${persona}/${name}: ${err}`); }
  await page.screenshot({ path: join(dir, `${name}.png`) });
  console.log(`  ✓ ${persona}/${name}.png`);
}

// Detect a stray error/warning surface visible in the shot.
async function detectError(page) {
  return await page.evaluate(() => {
    const texts = [];
    document.querySelectorAll('.o_dialog .modal-content, .o_error_dialog, .modal.show .modal-content').forEach(d => {
      const t = (d.querySelector('.modal-title, header')?.textContent || '').trim();
      if (/error|warning|invalid|access/i.test(t)) texts.push('dialog:' + t);
    });
    document.querySelectorAll('.o_notification.border-danger, .o_notification.border-warning').forEach(n => {
      texts.push('toast:' + (n.textContent || '').trim().slice(0, 80));
    });
    document.querySelectorAll('.o_form_view .alert-danger, .o_form_view .alert-warning').forEach(a => {
      texts.push('banner:' + (a.textContent || '').trim().slice(0, 90));
    });
    return texts.join(' | ');
  }).catch(() => '');
}

const only = process.argv[2];
const personas = only ? [only] : Object.keys(SHOTS);

const browser = await chromium.launch();
let ok = 0, fail = 0;
const errs = [];

// common shots (login page + app switcher) — captured once via employee session
if (!only || only === 'common' || only === 'employee') {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  try {
    mkdirSync(join(SHOTDIR, 'common'), { recursive: true });
    await page.goto(`${BASE}/web/login`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    await page.screenshot({ path: join(SHOTDIR, 'common', 'login-01.png') });
    console.log('  ✓ common/login-01.png'); ok++;
    await login(page, 'employee');
    await page.goto(`${BASE}/odoo`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: join(SHOTDIR, 'common', 'nav-01.png') });
    console.log('  ✓ common/nav-01.png'); ok++;
  } catch (e) { console.log(`  ✗ common: ${e.message.split('\n')[0]}`); fail++; }
  await ctx.close();
}

for (const persona of personas) {
  if (!SHOTS[persona]) continue;
  console.log(`\n[${persona}]`);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  try {
    await login(page, persona);
    for (const [name, action] of SHOTS[persona]) {
      try { await shoot(page, persona, name, action); ok++; }
      catch (e) { console.log(`  ✗ ${persona}/${name}: ${e.message.split('\n')[0]}`); fail++; }
    }
  } catch (e) { console.log(`  ✗ ${persona} login: ${e.message.split('\n')[0]}`); fail++; }
  await ctx.close();
}

await browser.close();
console.log(`\nDone. captured=${ok} failed=${fail}`);
if (errs.length) {
  console.log(`\n⚠ STRAY ERRORS DETECTED (${errs.length}):`);
  for (const e of errs) console.log('   - ' + e);
} else {
  console.log('✓ No stray error/warning surfaces detected in any landing shot.');
}
