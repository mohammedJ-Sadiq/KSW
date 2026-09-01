"""
Create per-persona demo login users (and a little sample data) in the DEV
database so the screenshot pipeline can log in as each role and capture the
role-gated UI.

Run against the DEV database only:

    python odoo-bin shell -c KSW_dev.conf --no-http < \
        custom_addons/KSW/docs/training/tools/setup_demo_data.py

Idempotent: re-running updates the users/groups rather than duplicating them.
All demo logins share the dev-only password below. These records live in the
dev DB only and are not part of committed module code. To remove them, unlink
the res.users with login like 'train.%' and the 'Train %' employees.
"""
import logging

_logger = logging.getLogger("ksw.training.setup")

PASSWORD = "trainKSW#2026"   # dev-only

PERSONAS = {
    "employee": [
        "base.group_user",
        "KSW_annual_leave.group_leave_self",
        "KSW_deduction.group_deduction_user",
        "KSW_payroll.group_hr_payroll_self",
    ],
    "supervisor": [
        "base.group_user",
        "KSW_base_security.group_hr_employee_supervisor",
        "KSW_annual_leave.group_leave_supervisor",
        "KSW_deduction.group_deduction_supervisor",
        "KSW_payroll.group_hr_payroll_supervisor",
        "KSW_commissions.group_commission_supervisor",
        "KSW_attendance_sheet.group_attendance_sheet_supervisor",
    ],
    "hr": [
        "base.group_user",
        "hr.group_hr_user",
        "KSW_annual_leave.group_annual_leave_hr",
        "KSW_deduction.group_loan_hr",
        "KSW_attendance_sheet.group_attendance_sheet_manager",
    ],
    "accounting": [
        "base.group_user",
        "KSW_annual_leave.group_annual_leave_acc",
        "KSW_deduction.group_loan_acc",
        "KSW_deduction.group_loan_disbursement",
        "KSW_deduction.group_installment_edit",
        "KSW_deduction.group_acc_data_entry",
        "KSW_commissions.group_commission_accountant",
    ],
    "gm": [
        "base.group_user",
        "KSW_annual_leave.group_annual_leave_gm",
        "KSW_deduction.group_loan_gm",
        "KSW_commissions.group_commission_gm",
    ],
    "payroll": [
        "base.group_user",
        "om_hr_payroll.group_hr_payroll_user",
    ],
    "it": [
        "base.group_user",
        "KSW_helpdesk.group_helpdesk_agent",
    ],
    "admin": [
        "base.group_user",
        "KSW_deduction.group_deduction_manager",
        "KSW_deduction.group_loan_edit_delete",
        "KSW_commissions.group_commission_officer",
        "KSW_commissions.group_sales_commission_manager",
    ],
}


def group_ids(xmlids):
    ids = []
    for x in xmlids:
        g = env.ref(x, raise_if_not_found=False)
        if g:
            ids.append(g.id)
        else:
            _logger.warning("training setup: missing group %s", x)
    return ids


# ---- 1) users -----------------------------------------------------------
users = {}
for persona, groups in PERSONAS.items():
    login = "train.%s" % persona
    vals = {
        "name": "Train %s" % persona.title(),
        "login": login,
        "password": PASSWORD,
        "group_ids": [(6, 0, group_ids(groups))],
    }
    user = env["res.users"].with_context(no_reset_password=True).search(
        [("login", "=", login)], limit=1)
    if user:
        user.write(vals)
    else:
        user = env["res.users"].with_context(no_reset_password=True).create(vals)
    users[persona] = user
    _logger.info("training setup: user %s -> id %s", login, user.id)

env.cr.commit()
print("USERS:", {p: u.id for p, u in users.items()})

# ---- 2) demo employees (supervisor -> employee reporting line) ----------
Emp = env["hr.employee"]


def ensure_emp(name, user, parent=None):
    e = Emp.search([("name", "=", name)], limit=1)
    vals = {"name": name, "user_id": user.id}
    if parent:
        vals["parent_id"] = parent.id
    if e:
        e.write(vals)
    else:
        e = Emp.create(vals)
    return e


try:
    sup_emp = ensure_emp("Train Supervisor", users["supervisor"])
    emp_emp = ensure_emp("Train Employee", users["employee"], parent=sup_emp)
    if "leave_manager_id" in Emp._fields:
        emp_emp.leave_manager_id = users["supervisor"]
    env.cr.commit()
    print("EMPLOYEES:", {"supervisor": sup_emp.id, "employee": emp_emp.id})
except Exception as exc:  # noqa: BLE001
    env.cr.rollback()
    _logger.warning("training setup: employee creation skipped: %s", exc)
    print("EMPLOYEES: skipped (%s)" % exc)

# ---- 3) minimal sample records (best-effort) ----------------------------
# A loan request so "My Loans" / approver lists are not empty.
try:
    loan_type = env["ksw.deduction.type"].search(
        [("is_loan", "=", True)], limit=1)
    existing = env["ksw.deduction"].search(
        [("employee_id", "=", emp_emp.id), ("is_loan", "=", True)], limit=1)
    if loan_type and not existing:
        env["ksw.deduction"].create({
            "employee_id": emp_emp.id,
            "type_id": loan_type.id,
            "amount": 5000.0,
            "installments": 10,
        })
        env.cr.commit()
        print("SAMPLE LOAN: created")
    else:
        print("SAMPLE LOAN: exists or no loan type (skipped)")
except Exception as exc:  # noqa: BLE001
    env.cr.rollback()
    _logger.warning("training setup: sample loan skipped: %s", exc)
    print("SAMPLE LOAN: skipped (%s)" % exc)

print("SETUP COMPLETE")
