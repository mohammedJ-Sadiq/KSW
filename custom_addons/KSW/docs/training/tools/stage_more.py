"""Stage remaining sample data: mark the demo employee as attendance-sheet based
(so the supervisor commission flow is visible) and create an HR salary advance
with installments (for the mark-paid screenshot). Merges ids into _demo_ids.json."""
import json
import os

JSON_PATH = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
emp = env["hr.employee"].search([("name", "=", "Train Employee")], limit=1)
ids = json.load(open(JSON_PATH)) if os.path.exists(JSON_PATH) else {}

# attendance-sheet based so supervisor's commission sheet is visible
try:
    if "x_is_attendance_sheet" in emp._fields:
        emp.sudo().write({"x_is_attendance_sheet": True})
    env.cr.commit()
    print("emp x_is_attendance_sheet set")
except Exception as exc:  # noqa: BLE001
    env.cr.rollback(); print("attendance flag skipped:", exc)

# HR salary advance (type id 2) with 3 installments -> for mark-paid shot
try:
    adv_type = env["ksw.deduction.type"].browse(2)
    adv = env["ksw.deduction"].search(
        [("employee_id", "=", emp.id), ("type_id", "=", adv_type.id)], limit=1)
    if not adv:
        adv = env["ksw.deduction"].create({
            "employee_id": emp.id, "type_id": adv_type.id,
            "amount": 1500.0, "installments": 3,
        })
    if not adv.line_ids:
        adv.sudo()._generate_installment_lines()
    adv.sudo().write({"state": "active"})
    ids["advance"] = adv.id
    env.cr.commit()
    print("advance id", adv.id, "lines", len(adv.line_ids))
except Exception as exc:  # noqa: BLE001
    env.cr.rollback(); print("advance skipped:", exc)

json.dump(ids, open(JSON_PATH, "w"), indent=2)
print("MERGED ids:", sorted(ids))
