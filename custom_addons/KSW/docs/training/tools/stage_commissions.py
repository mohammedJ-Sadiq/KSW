"""Stage the rebuilt commission app for the training screenshots (DEV only).

Run through `odoo-bin shell` — it commits per step, and each step is
independent so a failure in one does not cost the others.

What the screenshots need, and therefore what this builds:

* a supervisor who can actually record — the per-component **entry groups are
  opt-in** and not implied by the Supervisor tier, and his department needs a
  `manager_id`, or he correctly sees nothing at all;
* a GM who owns that department, so the approval buttons render for him;
* a **draft** Overtime batch with several rows (the entry screen);
* a **recurring** entry the supervisor can pull (the Add Recurring shot);
* a **submitted** department handover (the GM's By Department list);
* an **approved** month is deliberately NOT staged: approving locks the period
  and settles real loan installments. The accounting shots use the submitted
  month, and the export button is shot from the GM/officer view instead.

Idempotent: re-running finds what it made last time.
"""
import json
import os

JSON_PATH = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
ids = json.load(open(JSON_PATH)) if os.path.exists(JSON_PATH) else {}

PERIOD = "2026-08-01"


def ref(xmlid):
    return env.ref(xmlid, raise_if_not_found=False)


def commit(label, fn):
    try:
        out = fn()
        env.cr.commit()
        print(f"OK   {label}: {out}")
        return out
    except Exception as exc:                                    # noqa: BLE001
        env.cr.rollback()
        print(f"SKIP {label}: {str(exc)[:160]}")
        return None


# ── 1. the supervisor: entry groups + a department he manages ─────────────
def setup_supervisor():
    user = env["res.users"].search([("login", "=", "train.supervisor")], limit=1)
    if not user:
        raise Exception("train.supervisor does not exist — run setup_demo_data.py")
    groups = [g for g in (
        ref("KSW_commissions.group_commission_supervisor"),
        ref("KSW_commissions.group_entry_overtime"),
        ref("KSW_commissions.group_entry_driver"),
        ref("KSW_commissions.group_entry_location"),
    ) if g]
    user.sudo().write({"group_ids": [(4, g.id) for g in groups]})

    emp = env["hr.employee"].sudo().search([("user_id", "=", user.id)], limit=1)
    if not emp:
        raise Exception("train.supervisor has no employee record")
    dept = emp.department_id
    if not dept:
        dept = env["hr.department"].sudo().search(
            [("name", "=", "Train Department")], limit=1)
        if not dept:
            dept = env["hr.department"].sudo().create({"name": "Train Department"})
        emp.sudo().write({"department_id": dept.id})
    dept.sudo().write({"manager_id": emp.id})

    # somebody to pay: the demo employee reports to him, in the same department
    team = env["hr.employee"].sudo().search(
        [("name", "=", "Train Employee")], limit=1)
    if team:
        team.sudo().write({"department_id": dept.id, "parent_id": emp.id})
        if team.current_version_id and not team.current_version_id.wage:
            team.current_version_id.sudo().write({"wage": 6000.0})
    ids["comm_dept"] = dept.id
    ids["comm_supervisor_emp"] = emp.id
    if team:
        ids["comm_team_emp"] = team.id
    return f"dept={dept.name} mgr={emp.name} team={team.name if team else '-'}"


# ── 2. the GM of that department ──────────────────────────────────────────
def setup_gm():
    user = env["res.users"].search([("login", "=", "train.gm")], limit=1)
    if not user:
        raise Exception("train.gm does not exist")
    g = ref("KSW_commissions.group_commission_gm")
    if g:
        user.sudo().write({"group_ids": [(4, g.id)]})
    emp = env["hr.employee"].sudo().search([("user_id", "=", user.id)], limit=1)
    dept = env["hr.department"].sudo().browse(ids.get("comm_dept") or 0)
    if emp and dept.exists() and "x_gm_id" in dept._fields:
        dept.sudo().write({"x_gm_id": emp.id})
    return f"gm={emp.name if emp else '?'} on {dept.name if dept.exists() else '?'}"


# ── 3. a draft Overtime batch with rows ───────────────────────────────────
def stage_batch():
    comp = ref("KSW_commissions.pay_component_overtime")
    dept = env["hr.department"].sudo().browse(ids["comm_dept"])
    Batch = env["ksw.pay.batch"].sudo()
    batch = Batch.search([
        ("component_id", "=", comp.id), ("period", "=", PERIOD),
        ("department_id", "=", dept.id)], limit=1)
    if not batch:
        batch = Batch.create({
            "component_id": comp.id, "period": PERIOD,
            "department_id": dept.id})
    if not batch.entry_ids:
        emp_id = ids.get("comm_team_emp") or ids["comm_supervisor_emp"]
        rows = [
            ("2026-08-03", 3.0, "Emergency generator repair"),
            ("2026-08-11", 2.5, "Client site call-out"),
            ("2026-08-19", 4.0, "Month-end shutdown cover"),
        ]
        env["ksw.pay.entry"].sudo().create([{
            "batch_id": batch.id, "employee_id": emp_id,
            "date": d, "quantity": q, "reason": r,
        } for d, q, r in rows])
    ids["comm_batch_draft"] = batch.id
    return f"{batch.name} entries={len(batch.entry_ids)} total={batch.total_amount:.2f}"


# ── 4. a recurring entry to pull ──────────────────────────────────────────
def stage_recurring():
    comp = ref("KSW_commissions.pay_component_mobile")
    emp_id = ids.get("comm_team_emp") or ids["comm_supervisor_emp"]
    Rec = env["ksw.pay.recurring"].sudo()
    rec = Rec.search([("employee_id", "=", emp_id),
                      ("component_id", "=", comp.id)], limit=1)
    if not rec:
        rec = Rec.create({
            "employee_id": emp_id, "component_id": comp.id,
            "amount": 150.0, "date_from": "2026-01-01",
            "reason": "Company mobile line"})
    ids["comm_recurring"] = rec.id
    return f"{rec.display_name} amount={rec.amount}"


# ── 5. a submitted department handover ────────────────────────────────────
def stage_submission():
    batch = env["ksw.pay.batch"].sudo().browse(ids["comm_batch_draft"])
    sub = batch.submission_id
    if not sub:
        raise Exception("batch has no submission")
    if sub.state == "draft" and batch.entry_ids:
        sub.sudo().action_submit()
    ids["comm_submission"] = sub.id
    ids["comm_run"] = sub.run_id.id
    return f"{sub.display_name} state={sub.state} run={sub.run_id.display_name}"


for label, fn in [
    ("supervisor", setup_supervisor),
    ("gm", setup_gm),
    ("draft batch", stage_batch),
    ("recurring", stage_recurring),
    ("submission", stage_submission),
]:
    commit(label, fn)

json.dump(ids, open(JSON_PATH, "w"), indent=2)
print("MERGED:", sorted(k for k in ids if k.startswith("comm_")))
