/*
 * Screenshots for the "Vehicles & Staff" handbooks (DEV only): Vehicle
 * Allocation, Vehicle Custody (HR-12), Employee Transfers, Documents and the
 * three workshops. Records come from stage_vehicles.py (_demo_ids.json, veh_*).
 *
 *   node capture_vehicles.mjs            # English  -> ../screenshots/
 *   KSW_LANG=ar node capture_vehicles.mjs  # Arabic -> ../screenshots-ar/
 *   node capture_vehicles.mjs <tag>      # one shot, e.g. supervisor/dc-03
 *
 * The Arabic pass needs the train.* users in Arabic first (and back after):
 *   set_train_lang.py with KSW_DEMO_LANG=ar / en.
 * Each shot is best-effort: a failure is logged and the run goes on.
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { mkdirSync, readFileSync } from 'fs';

const BASE = process.env.KSW_URL || 'http://localhost:8070';
const PASSWORD = process.env.KSW_PW || 'trainKSW#2026';
const AR = process.env.KSW_LANG === 'ar';
const __dirname = dirname(fileURLToPath(import.meta.url));
const SHOTDIR = join(__dirname, '..', AR ? 'screenshots-ar' : 'screenshots');
const IDS = JSON.parse(readFileSync(join(__dirname, '_demo_ids.json'), 'utf8'));
const T = (en, ar) => (AR ? ar : en);

const A = {
  release: 'KSW_fleet_allocation.action_ksw_fleet_request_release',
  addition: 'KSW_fleet_allocation.action_ksw_fleet_request_addition',
  toDecide: 'KSW_fleet_allocation.action_ksw_fleet_request_to_decide',
  allRequests: 'KSW_fleet_allocation.action_ksw_fleet_request',
  vehDepts: 'KSW_fleet_allocation.action_ksw_fleet_allocation_departments',
  deptUsers: 'KSW_fleet_allocation.action_ksw_fleet_department_users',
  custody: 'KSW_vehicle_custody.action_ksw_vehicle_custody',
  driverChange: 'KSW_vehicle_custody.action_ksw_vehicle_custody_driver_change',
  whoAnswers: 'KSW_vehicle_custody.action_ksw_fleet_vehicle_custody',
  empRelease: 'KSW_employee_transfer.action_ksw_employee_release',
  empAddition: 'KSW_employee_transfer.action_ksw_employee_addition',
  empPool: 'KSW_employee_transfer.action_ksw_employee_release_pool',
  empRequests: 'KSW_employee_transfer.action_ksw_employee_request',
  docRequests: 'KSW_documents.action_ksw_document_request',
  myDocRequests: 'KSW_documents.action_ksw_document_request_my',
  imports: 'KSW_documents.action_ksw_document_import',
  docTypes: 'KSW_documents.action_ksw_document_type',
  wsAll: 'KSW_workshop.action_ksw_workshop_request_all',
  workshops: 'KSW_workshop.action_ksw_workshop',
};

async function login(page, persona) {
  await page.goto(`${BASE}/web/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="login"]', `train.${persona}`);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForSelector('.o_main_navbar', { timeout: 30000 });
}
async function list(page, action) {
  await page.goto(`${BASE}/odoo/action-${action}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.o_list_view, .o_kanban_view, .o_form_view', { timeout: 20000 });
  await page.waitForTimeout(1200);
}
async function form(page, action, id) {
  await page.goto(`${BASE}/odoo/action-${action}/${id}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.o_form_view', { timeout: 20000 });
  await page.waitForTimeout(1200);
}
async function clickButton(page, en, ar) {
  await page.getByRole('button', { name: T(en, ar), exact: true }).first().click();
  await page.waitForTimeout(1500);
}
async function clickTab(page, en, ar) {
  await page.locator('.o_notebook .nav-link', { hasText: T(en, ar) }).first().click();
  await page.waitForTimeout(800);
}
async function save(page, persona, name) {
  const dir = join(SHOTDIR, persona);
  mkdirSync(dir, { recursive: true });
  await page.waitForTimeout(800);
  await page.screenshot({ path: join(dir, `${name}.png`) });
  console.log(`  ✓ ${persona}/${name}.png`);
}

const SHOTS = [
  // ---------------- supervisor ----------------
  { tag: 'supervisor/veh-01', who: 'supervisor', run: p => list(p, A.release) },
  { tag: 'supervisor/veh-02', who: 'supervisor', run: p => form(p, A.release, IDS.veh_release) },
  { tag: 'supervisor/veh-03', who: 'supervisor', run: async p => {
      await list(p, A.addition);
      await p.locator('.o_list_button_add, .o-kanban-button-new').first().click();
      await p.waitForSelector('.o_form_view');
  } },
  { tag: 'supervisor/emp-01', who: 'supervisor', run: p => list(p, A.empRelease) },
  { tag: 'supervisor/emp-02', who: 'supervisor', run: p => form(p, A.empRelease, IDS.veh_emp_release) },
  { tag: 'supervisor/emp-03', who: 'supervisor', run: async p => {
      await form(p, A.empRelease, IDS.veh_emp_release);
      await clickTab(p, 'Vehicles in Their Custody', 'المركبات في عهدتهم');
  } },
  { tag: 'supervisor/dc-01', who: 'supervisor', run: p => list(p, A.driverChange) },
  { tag: 'supervisor/dc-02', who: 'supervisor', run: p => form(p, A.driverChange, IDS.veh_dc_draft) },
  { tag: 'supervisor/dc-03', who: 'supervisor', run: async p => {
      await form(p, A.driverChange, IDS.veh_dc_nolicence);
      await p.locator('button[name="action_submit"]:visible').first().click();
      await p.waitForTimeout(1200);
      const ok = p.locator('.modal-footer button.btn-primary').first();   // "Confirm?" question
      if (await ok.count()) { await ok.click(); }
      await p.waitForTimeout(2500);
  } },
  { tag: 'supervisor/dc-05', who: 'supervisor', run: async p => {
      await form(p, A.driverChange, IDS.veh_dc_draft);
      await p.locator('button[name="action_submit"]:visible').first().click();
      await p.waitForTimeout(1200);
      const ok = p.locator('.modal-footer button.btn-primary').first();
      if (await ok.count()) { await ok.click(); }
      await p.waitForTimeout(2500);
  } },
  { tag: 'supervisor/dc-04', who: 'supervisor', run: p => form(p, A.driverChange, IDS.veh_dc_done) },
  { tag: 'supervisor/docreq-01', who: 'supervisor', run: p => list(p, A.myDocRequests) },
  { tag: 'supervisor/docreq-02', who: 'supervisor', run: p => form(p, A.myDocRequests, IDS.veh_doc_request) },
  // ---------------- GM ----------------
  { tag: 'gm/veh-01', who: 'gm', run: p => list(p, A.toDecide) },
  { tag: 'gm/veh-02', who: 'gm', run: async p => {
      await form(p, A.toDecide, IDS.veh_release);
      await p.locator('button[name="action_decide_release_lines"]').first().click();
      await p.waitForSelector('.modal .o_form_view', { timeout: 15000 });
  } },
  { tag: 'gm/veh-03', who: 'gm', run: async p => {
      await form(p, A.toDecide, IDS.veh_addition);
      await p.locator('.o_field_one2many button[name="action_open_decision"]').first().click();
      await p.waitForSelector('.modal .o_form_view', { timeout: 15000 });
  } },
  { tag: 'gm/emp-01', who: 'gm', run: p => list(p, A.empPool) },
  { tag: 'gm/emp-02', who: 'gm', run: async p => {
      await form(p, A.empRequests, IDS.veh_emp_release);
      await clickButton(p, 'Decide', 'البت');
      await p.waitForSelector('.modal .o_form_view', { timeout: 15000 });
  } },
  { tag: 'gm/emp-03', who: 'gm', run: async p => {
      await form(p, A.empRequests, IDS.veh_emp_addition);
      await p.locator('.o_field_one2many button[name="action_open_decide"]').first().click();
      await p.waitForSelector('.modal .o_form_view', { timeout: 15000 });
  } },
  // ---------------- HR ----------------
  { tag: 'hr/veh-01', who: 'hr', run: p => list(p, A.custody) },
  { tag: 'hr/veh-02', who: 'hr', run: p => form(p, A.custody, IDS.veh_handin) },
  { tag: 'hr/veh-03', who: 'hr', run: async p => {
      await form(p, A.custody, IDS.veh_handin);
      await clickTab(p, 'HR / Administrative Affairs', 'الموارد البشرية / الشؤون الإدارية');
  } },
  { tag: 'hr/doc-01', who: 'hr', run: p => list(p, A.docRequests) },
  { tag: 'hr/doc-02', who: 'hr', run: p => form(p, A.docRequests, IDS.veh_doc_request) },
  { tag: 'hr/doc-03', who: 'hr', run: async p => {
      await list(p, A.imports);
      await p.locator('.o_list_button_add').first().click();
      await p.waitForSelector('.o_form_view');
  } },
  { tag: 'hr/doc-04', who: 'admin', run: p => list(p, A.docTypes) },
  { tag: 'hr/emp-01', who: 'hr', run: p => list(p, A.empRequests) },
  // ---------------- workshop ----------------
  { tag: 'workshop/ws-01', who: 'workshop', run: p => list(p, A.wsAll) },
  { tag: 'workshop/ws-02', who: 'workshop', run: p => form(p, A.custody, IDS.veh_handin) },
  { tag: 'workshop/ws-03', who: 'workshop', run: async p => {
      await form(p, A.custody, IDS.veh_handin);
      await clickTab(p, 'Vehicle Inspection', 'فحص المركبة');
  } },
  // ---------------- administrator ----------------
  { tag: 'admin/veh-01', who: 'admin', run: async p => {
      await list(p, A.vehDepts);
      await p.locator('.o_searchview_input').fill('Training');
      await p.keyboard.press('Enter');
      await p.waitForTimeout(1500);
  } },
  { tag: 'admin/veh-02', who: 'admin', run: p => list(p, A.deptUsers) },
  { tag: 'admin/veh-03', who: 'admin', run: p => list(p, A.workshops) },
];

const only = process.argv[2];
const browser = await chromium.launch();
const byWho = {};
for (const s of SHOTS.filter(s => !only || s.tag === only)) (byWho[s.who] ||= []).push(s);
for (const [who, shots] of Object.entries(byWho)) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: AR ? 'ar' : 'en-US' });
  const page = await ctx.newPage();
  try { await login(page, who); } catch (e) { console.log(`  ✗ login ${who}: ${e.message}`); await ctx.close(); continue; }
  for (const s of shots) {
    try {
      await s.run(page);
      const [persona, name] = s.tag.split('/');
      await save(page, persona, name);
    } catch (e) {
      console.log(`  ✗ ${s.tag}: ${e.message.split('\n')[0]}`);
    }
    await page.keyboard.press('Escape').catch(() => {});
  }
  await ctx.close();
}
await browser.close();
