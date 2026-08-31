/*
 * Deep/interaction screenshots for the KSW training manual (DEV only).
 * Companion to capture_screenshots.mjs — this one performs clicks (open a
 * wizard, a dialog, a dropdown, a fresh form) before shooting.
 *
 *   node capture_deep.mjs           # run all defined deep shots
 *   node capture_deep.mjs <tag>      # run one shot by its "persona/name" tag
 *
 * Each shot is best-effort: failures are logged and the run continues.
 */
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { mkdirSync, readFileSync, existsSync } from 'fs';

const BASE = process.env.KSW_URL || 'http://localhost:8070';
const PASSWORD = process.env.KSW_PW || 'trainKSW#2026';
const __dirname = dirname(fileURLToPath(import.meta.url));
const SHOTDIR = join(__dirname, '..', 'screenshots');

// record ids staged by stage_records.py (loans per approval state, etc.)
const IDS = existsSync(join(__dirname, '_demo_ids.json'))
  ? JSON.parse(readFileSync(join(__dirname, '_demo_ids.json'), 'utf8')) : {};
const LOANS = 'KSW_deduction.action_ksw_deduction_loans';
const LEAVES = 'hr_holidays.hr_leave_action_action_approve_department';
async function openRecord(page, action, id) {
  await page.goto(`${BASE}/odoo/action-${action}/${id}`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.o_form_view', { timeout: 15000 });
}

async function login(page, persona) {
  await page.goto(`${BASE}/web/login`, { waitUntil: 'domcontentloaded' });
  await page.fill('input[name="login"]', `train.${persona}`);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForSelector('.o_main_navbar', { timeout: 30000 });
}
async function gotoAction(page, action) {
  await page.goto(`${BASE}/odoo/action-${action}`, { waitUntil: 'domcontentloaded' });
}
// Discard a dirty NEW form so it is not silently auto-saved on the next
// navigation (Odoo persists a valid new record on nav even with a soft
// overlap warning). Call as a shot's `post` hook, after the screenshot.
async function discardForm(page) {
  const btn = page.locator('.o_form_button_cancel').first();
  if (await btn.count()) { await btn.click().catch(() => {}); await page.waitForTimeout(400); }
  // confirm any "discard changes?" dialog
  const ok = page.getByRole('button', { name: /^(Discard|Ok|Yes)$/i }).first();
  if (await ok.count()) { await ok.click().catch(() => {}); }
  await page.waitForTimeout(300);
}
// click an app in the left sidebar so the app chrome (top menu) is correct
async function openSidebarApp(page, name) {
  await page.locator('a.nav-link', { hasText: name }).first().click();
  await page.waitForSelector('.o_list_view, .o_kanban_view', { timeout: 15000 });
  await page.waitForTimeout(1200);
}
async function save(page, persona, name) {
  const dir = join(SHOTDIR, persona);
  mkdirSync(dir, { recursive: true });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: join(dir, `${name}.png`) });
  console.log(`  ✓ ${persona}/${name}.png`);
}

// Detect a stray error/warning surface visible in the shot (Access Error dialog,
// server traceback modal, red danger notification, or a leave-overlap banner).
// Returns a short description string, or '' if the screen looks clean.
async function detectError(page) {
  return await page.evaluate(() => {
    const texts = [];
    // Odoo error/warning dialogs
    document.querySelectorAll('.o_dialog .modal-content, .o_error_dialog, .modal.show .modal-content').forEach(d => {
      const t = (d.querySelector('.modal-title, header')?.textContent || '').trim();
      if (/error|warning|invalid|access/i.test(t)) texts.push('dialog:' + t);
    });
    // danger/warning toast notifications
    document.querySelectorAll('.o_notification.border-danger, .o_notification.border-warning, .o_notification_bar.bg-danger').forEach(n => {
      texts.push('toast:' + (n.textContent || '').trim().slice(0, 80));
    });
    // inline validation/overlap banner on forms
    document.querySelectorAll('.o_form_view .alert-danger, .o_form_view .alert-warning').forEach(a => {
      const t = (a.textContent || '').trim();
      // the skipped-employees panel is intended, documented batch content
      if (/automatically skipped during payslip generation/i.test(t)) return;
      texts.push('banner:' + t.slice(0, 90));
    });
    return texts.join(' | ');
  }).catch(() => '');
}

// each: { tag:'persona/name', persona, run:async(page)=>{} }  (run leaves the
// target screen ready; save() is called after)
const SHOTS = [
  // ── the rebuilt commission app (Aug 2026) ───────────────────────────────
  // Data staged by stage_commissions.py. Two batches on purpose: the Overtime
  // one is submitted with its department (frozen — the handover shots), the
  // Meals one is draft, which is the only way to shoot an editable entry grid
  // with the Type column showing.
  { tag: 'supervisor/pay-01', persona: 'supervisor', run: async (page) => {
      await gotoAction(page, 'KSW_commissions.action_ksw_pay_batch');
      await page.waitForSelector('.o_list_view', { timeout: 15000 });
      await page.waitForTimeout(900);
  }},
  { tag: 'supervisor/pay-02', persona: 'supervisor', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-KSW_commissions.action_ksw_pay_batch/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 15000 });
      await page.waitForTimeout(1200);
  }},
  { tag: 'supervisor/pay-03', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_batch', IDS.comm_batch_meals);
      await page.waitForTimeout(1200);
  }},
  { tag: 'supervisor/pay-04', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_batch', IDS.comm_batch_draft);
      await page.waitForTimeout(900);
      const calc = page.locator('button[name="action_explain"]').first();
      if (await calc.count()) { await calc.click(); await page.waitForSelector('.modal', { timeout: 10000 }); }
      await page.waitForTimeout(800);
  }},
  { tag: 'supervisor/recurring-01', persona: 'supervisor', run: async (page) => {
      await gotoAction(page, 'KSW_commissions.action_ksw_pay_recurring');
      await page.waitForSelector('.o_list_view', { timeout: 15000 });
      await page.waitForTimeout(900);
  }},
  { tag: 'supervisor/recurring-02', persona: 'supervisor', run: async (page) => {
      await gotoAction(page, 'KSW_commissions.action_ksw_pay_recurring');
      await page.waitForSelector('.o_list_view', { timeout: 15000 });
      const row = page.locator('.o_data_row').first();
      if (await row.count()) { await row.locator('td').nth(1).click(); await page.waitForTimeout(900); }
  }},
  { tag: 'supervisor/recurring-03', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_batch', IDS.comm_batch_meals);
      await page.waitForTimeout(1000);
  }},
  { tag: 'supervisor/payrun-01', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(900);
      const tab = page.locator('a.nav-link', { hasText: /Who Gets Paid|يُصرَف/ }).first();
      if (await tab.count()) { await tab.click(); await page.waitForTimeout(900); }
  }},
  { tag: 'supervisor/payrun-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(1000);
  }},
  { tag: 'gm/comm-01', persona: 'gm', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(1200);
  }},
  { tag: 'gm/comm-02', persona: 'gm', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(900);
      const tab = page.locator('a.nav-link', { hasText: /Who Gets Paid|يُصرَف/ }).first();
      if (await tab.count()) { await tab.click(); await page.waitForTimeout(900); }
  }},
  { tag: 'gm/comm-03', persona: 'gm', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(1000);
  }},
  { tag: 'gm/comm-04', persona: 'gm', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_submission', IDS.comm_submission);
      await page.waitForTimeout(1200);
  }},
  { tag: 'accounting/comm-01', persona: 'accounting', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_pay_run', IDS.comm_run);
      await page.waitForTimeout(1000);
  }},
  { tag: 'accounting/comm-02', persona: 'accounting', run: async (page) => {
      await gotoAction(page, 'KSW_commissions.action_ksw_pay_run_line');
      await page.waitForSelector('.o_list_view', { timeout: 15000 });
      await page.waitForTimeout(900);
  }},
  { tag: 'admin/comp-01', persona: 'admin', run: async (page) => {
      await gotoAction(page, 'KSW_commissions.action_ksw_pay_component');
      await page.waitForSelector('.o_list_view', { timeout: 15000 });
      const row = page.locator('.o_data_row').first();
      if (await row.count()) { await row.click(); await page.waitForSelector('.o_form_view', { timeout: 12000 }); }
      await page.waitForTimeout(900);
  }},
  { tag: 'common/language-01', persona: 'employee', run: async (page) => {
      await page.click('.o_user_menu');
      await page.waitForTimeout(500);
      await page.click('text=My Preferences');
      await page.waitForTimeout(2500);   // opens as dialog or fullpage form
  }},
  { tag: 'common/inbox-01', persona: 'employee', run: async (page) => {
      await gotoAction(page, 'mail.action_discuss');
      await page.waitForSelector('.o-mail-Discuss, .o_mail_discuss', { timeout: 12000 });
  }},
  { tag: 'common/waiting-for-me-01', persona: 'accounting', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_deduction_loans');
      await page.waitForSelector('.o_list_view, .o_kanban_view', { timeout: 15000 });
      await page.click('.o_searchview_dropdown_toggler, .o_searchview button');
      await page.waitForTimeout(600);
  }},
  { tag: 'employee/loan-03', persona: 'employee', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_loan_request');
      await page.waitForSelector('.o_form_view, .modal', { timeout: 12000 });
  }},
  { tag: 'employee/loan-04', persona: 'employee', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_loan_request');
      await page.waitForSelector('.o_form_view, .modal', { timeout: 12000 });
      await page.evaluate(() => { const el=document.querySelector('.o_form_sheet, .modal-body'); if(el) el.scrollTop=el.scrollHeight; });
  }},
  { tag: 'employee/timeoff-02', persona: 'employee', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-hr_holidays.hr_leave_action_my/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      // open the Time Off Type dropdown to show the choices
      const fld = await page.$('.o_field_widget[name="holiday_status_id"] input');
      if (fld) { await fld.click(); await page.waitForTimeout(700); }
  }},
  { tag: 'employee/timeoff-03', persona: 'employee', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-hr_holidays.hr_leave_action_my/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      await page.waitForTimeout(600);
  }},
  { tag: 'hr/deduction-create-02', persona: 'hr', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_deduction_hr_managed');
      await page.waitForSelector('.o_list_view, .o_kanban_view', { timeout: 15000 });
      await page.click('button.o_list_button_add, .o-kanban-button-new, .o_control_panel_main button:has-text("New")');
      await page.waitForSelector('.o_form_view', { timeout: 10000 });
  }},
  { tag: 'admin/ded-types-01', persona: 'admin', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_deduction_type');
      await page.waitForSelector('.o_list_view, .o_kanban_view', { timeout: 15000 });
  }},

  // --- Phase 2: loan forms in a specific approval state (approve buttons) ---
  { tag: 'employee/loan-02', persona: 'employee', run: async (page) => {
      await gotoAction(page, 'KSW_deduction.action_ksw_my_loans');
      await page.waitForSelector('.o_list_view, .o_kanban_view', { timeout: 15000 });
  }},
  { tag: 'supervisor/loan-approve-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_pending_dm);
  }},
  { tag: 'hr/loan-hr-02', persona: 'hr', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_pending_hr);
  }},
  { tag: 'accounting/loan-acc-02', persona: 'accounting', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_pending_acc);
  }},
  { tag: 'accounting/loan-disburse-01', persona: 'accounting', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_pending_disbursement);
  }},
  { tag: 'accounting/loan-payment-01', persona: 'accounting', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_active);
  }},
  { tag: 'accounting/loan-payment-02', persona: 'accounting', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_active);
      await page.waitForTimeout(600);
      await page.getByRole('button', { name: 'Record Payment' }).first().click();
      await page.waitForTimeout(2800);   // wizard opens as dialog or full page
  }},
  { tag: 'gm/loan-gm-02', persona: 'gm', run: async (page) => {
      await openRecord(page, LOANS, IDS.loan_pending_gm);
  }},

  // --- Phase 4: annual-leave forms in a specific approval state ---
  { tag: 'supervisor/leave-approve-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_dm);
  }},
  { tag: 'hr/leave-hr-02', persona: 'hr', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_hr);
  }},
  { tag: 'hr/leave-hr-03', persona: 'hr', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_employee_signature);
  }},
  { tag: 'accounting/leave-acc-02', persona: 'accounting', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_acc);
  }},
  { tag: 'gm/leave-gm-02', persona: 'gm', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_gm_initial);
  }},
  { tag: 'gm/leave-gm-03', persona: 'gm', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_gm_final);
  }},
  { tag: 'gm/return-01', persona: 'gm', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_gm_initial);
  }},
  { tag: 'gm/return-02', persona: 'gm', run: async (page) => {
      await openRecord(page, LEAVES, IDS.leave_pending_gm_initial);
      await page.waitForTimeout(600);
      await page.getByRole('button', { name: /Return to Approver/i }).first().click();
      await page.waitForTimeout(2500);
  }},

  // --- Phase 3 + misc ---
  { tag: 'supervisor/comm-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_commission_sheet_my', IDS.commission_sheet);
  }},
  { tag: 'supervisor/comm-03', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_commission_sheet_my', IDS.commission_sheet);
      await page.waitForTimeout(500);
  }},
  { tag: 'employee/commission-02', persona: 'employee', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_commission_sheet_all', IDS.commission_sheet);
  }},
  { tag: 'hr/deduction-modify-01', persona: 'hr', run: async (page) => {
      await openRecord(page, 'KSW_deduction.action_ksw_deduction_hr_managed', IDS.advance);
      await page.waitForTimeout(500);
      await page.getByRole('tab', { name: /Installments/i }).first().click().catch(() => {});
      await page.waitForTimeout(800);
  }},
  { tag: 'payroll/batch-02', persona: 'payroll', run: async (page) => {
      await page.goto(`${BASE}/odoo/action-om_hr_payroll.action_hr_payslip_run_tree/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      await page.waitForTimeout(600);
  }},

  // --- Final batch: real payslip/batch records (read-only) + newly staged data ---
  { tag: 'employee/payslip-02', persona: 'payroll', run: async (page) => {
      await openRecord(page, 'om_hr_payroll.action_view_hr_payslip_form', IDS.payslip);
  }},
  { tag: 'supervisor/team-payslip-01', persona: 'payroll', run: async (page) => {
      await openRecord(page, 'om_hr_payroll.action_view_hr_payslip_form', IDS.payslip);
  }},
  { tag: 'payroll/export-01', persona: 'payroll', run: async (page) => {
      await openRecord(page, 'om_hr_payroll.action_hr_payslip_run_tree', IDS.payslip_batch);
      await page.getByRole('button', { name: /Refresh Totals/i }).first().click().catch(() => {});
      await page.waitForTimeout(1500);
  }},
  { tag: 'payroll/export-02', persona: 'payroll', run: async (page) => {
      await openRecord(page, 'om_hr_payroll.action_hr_payslip_run_tree', IDS.payslip_batch);
      await page.waitForTimeout(500);
      await page.getByRole('button', { name: /Export Bank File/i }).first().click();
      await page.waitForTimeout(2500);
  }},
  { tag: 'payroll/skipped-01', persona: 'payroll', run: async (page) => {
      await openRecord(page, 'om_hr_payroll.action_hr_payslip_run_tree', IDS.skip_batch);
      await page.waitForTimeout(600);
      // scroll the "Skipped" section into view
      await page.evaluate(() => {
        const el = [...document.querySelectorAll('*')].find(n =>
          /skipped/i.test(n.textContent || '') && n.children.length < 6 && n.offsetParent);
        if (el) el.scrollIntoView({ block: 'center' });
      });
      await page.waitForTimeout(800);
  }},
  { tag: 'supervisor/attsheet-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_attendance_sheet.action_ksw_attendance_sheet', IDS.att_sheet);
  }},
  // employee attendance: open the real Attendances APP (correct chrome), show own records
  { tag: 'employee/attendance-01', persona: 'employee', run: async (page) => {
      await openSidebarApp(page, 'Attendances');
  }},
  { tag: 'employee/attendance-02', persona: 'employee', run: async (page) => {
      await openSidebarApp(page, 'Attendances');
      // remove the default "Month > Employee" grouping to show individual records
      for (let i = 0; i < 3; i++) {
        const x = page.locator('.o_searchview .o_facet_remove').first();
        if (await x.count()) { await x.click(); await page.waitForTimeout(500); }
      }
      await page.waitForTimeout(1000);
  }},
  { tag: 'accounting/comm-02', persona: 'accounting', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_commission_sheet_all', IDS.commission_sheet);
  }},
  { tag: 'accounting/comm-03', persona: 'accounting', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_commission_batch', IDS.comm_batch);
  }},
  // ---- Helpdesk (KSW_helpdesk) -------------------------------------
  { tag: 'employee/ticket-02', persona: 'employee', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-KSW_helpdesk.action_helpdesk_ticket_my_tickets/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      await page.waitForTimeout(800);
  }},
  { tag: 'employee/ticket-03', persona: 'employee', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-KSW_helpdesk.action_helpdesk_ticket_my_tickets/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      await page.waitForTimeout(600);
      await page.evaluate(() => { const el = document.querySelector('.o_content'); if (el) el.scrollTop = el.scrollHeight; });
      await page.waitForTimeout(600);
  }},
  { tag: 'employee/ticket-04', persona: 'employee', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_helpdesk_ticket_my_tickets', IDS.ticket_new);
  }},
  { tag: 'supervisor/ticket-01', persona: 'supervisor', post: discardForm, run: async (page) => {
      await page.goto(`${BASE}/odoo/action-KSW_helpdesk.action_helpdesk_ticket_my_tickets/new`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.o_form_view', { timeout: 12000 });
      const fld = await page.$('.o_field_widget[name="employee_id"] input');
      if (fld) { await fld.click(); await fld.fill(''); await page.waitForTimeout(900); }
  }},
  { tag: 'supervisor/ticket-02', persona: 'supervisor', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_helpdesk_ticket_my_tickets', IDS.ticket_new);
  }},
  { tag: 'it/ticket-01', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_helpdesk_ticket_all', IDS.ticket_new);
  }},
  { tag: 'it/ticket-close-01', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_helpdesk_ticket_all', IDS.ticket_in_progress);
  }},
  { tag: 'it/ticket-closed-01', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_helpdesk_ticket_all', IDS.ticket_closed);
  }},
  { tag: 'it/queue-02', persona: 'it', run: async (page) => {
      await gotoAction(page, 'KSW_helpdesk.action_helpdesk_ticket_all');
      await page.waitForSelector('.o_kanban_view, .o_list_view', { timeout: 15000 });
      await page.click('.o_searchview_dropdown_toggler, .o_searchview button');
      await page.waitForTimeout(700);
  }},
  { tag: 'it/asset-02', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_it_asset_all', IDS.asset_laptop);
  }},
  { tag: 'it/assign-01', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_it_asset_all', IDS.asset_monitor);
      // "Assign to Employee" / "تسليم عهدة لموظف"
      await page.getByRole('button', { name: /Assign to Employee|تسليم عهدة/i }).first().click();
      await page.waitForSelector('.modal .o_form_view', { timeout: 12000 });
      await page.waitForTimeout(700);
  }},
  { tag: 'it/return-01', persona: 'it', run: async (page) => {
      await openRecord(page, 'KSW_helpdesk.action_it_asset_all', IDS.asset_laptop);
      // header "Return" / "استرجاع العهدة" (must not match "Return from Maintenance")
      await page.getByRole('button', { name: /^(Return|استرجاع العهدة)$/i }).first().click();
      await page.waitForSelector('.modal .o_form_view', { timeout: 12000 });
      await page.waitForTimeout(700);
  }},
  { tag: 'admin/comm-override-01', persona: 'accounting', run: async (page) => {
      await openRecord(page, 'KSW_commissions.action_ksw_sales_commission_sheet', IDS.sales_sheet);
  }},
];

const only = process.argv[2];
const list = only ? SHOTS.filter(s => s.tag === only) : SHOTS;
const browser = await chromium.launch();
let ok = 0, fail = 0;
const errs = [];
// group by persona to reuse a login
const byPersona = {};
for (const s of list) (byPersona[s.persona] ||= []).push(s);

for (const [persona, shots] of Object.entries(byPersona)) {
  console.log(`\n[${persona}]`);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  try {
    await login(page, persona);
    for (const s of shots) {
      try {
        await s.run(page);
        const [p, name] = s.tag.split('/');
        const err = await detectError(page);
        if (err) { console.log(`  ⚠ ERROR ON ${s.tag}: ${err}`); errs.push(`${s.tag}: ${err}`); }
        await save(page, p, name);
        if (s.post) await s.post(page).catch(() => {});
        ok++;
      } catch (e) { console.log(`  ✗ ${s.tag}: ${e.message.split('\n')[0]}`); fail++; }
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
  console.log('✓ No stray error/warning surfaces detected in any deep shot.');
}
