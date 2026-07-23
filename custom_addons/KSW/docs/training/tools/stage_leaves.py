"""Stage annual leaves in each approval state for the deep leave screenshots.
Run via odoo shell after setup_demo_data.py. Merges ids into _demo_ids.json.
DEV-only sample data."""
import json
import logging
import os
from datetime import date, timedelta

_logger = logging.getLogger("ksw.training.stage_leaves")
JSON_PATH = "custom_addons/KSW/docs/training/tools/_demo_ids.json"

emp = env["hr.employee"].search([("name", "=", "Train Employee")], limit=1)
ltype = env["hr.leave.type"].browse(77)
ids = {}
if os.path.exists(JSON_PATH):
    ids = json.load(open(JSON_PATH))

# validated allocation so the leaves pass balance checks
try:
    Alloc = env["hr.leave.allocation"]
    alloc = Alloc.search([("employee_id", "=", emp.id),
                          ("holiday_status_id", "=", ltype.id)], limit=1)
    if not alloc:
        alloc = Alloc.create({
            "name": "Training demo allocation",
            "holiday_status_id": ltype.id,
            "employee_id": emp.id,
            "number_of_days": 30,
        })
    if alloc.state != "validate":
        try:
            alloc.action_approve()
        except Exception:
            alloc.sudo().write({"state": "validate"})
    env.cr.commit()
    print("ALLOC ok id", alloc.id, "state", alloc.state)
except Exception as exc:  # noqa: BLE001
    env.cr.rollback()
    print("ALLOC skipped:", exc)

STATES = ["pending_dm", "pending_hr", "pending_gm_initial",
          "pending_acc", "pending_gm_final", "pending_employee_signature"]
Leave = env["hr.leave"]
base = date.today() + timedelta(days=10)
for i, st in enumerate(STATES):
    key = "leave_%s" % st
    d_from = base + timedelta(days=i * 7)
    d_to = d_from + timedelta(days=2)
    rec = Leave.search([("employee_id", "=", emp.id),
                        ("x_annual_approval_state", "=", st),
                        ("holiday_status_id", "=", ltype.id)], limit=1)
    try:
        if not rec:
            rec = Leave.with_context(
                leave_skip_state_check=True, leave_fast_create=True
            ).create({
                "holiday_status_id": ltype.id,
                "employee_id": emp.id,
                "request_date_from": d_from,
                "request_date_to": d_to,
            })
        rec.sudo().write({"x_annual_approval_state": st})
        ids[key] = rec.id
        env.cr.commit()
        print("LEAVE %s -> id %s" % (key, rec.id))
    except Exception as exc:  # noqa: BLE001
        env.cr.rollback()
        print("LEAVE %s SKIPPED: %s" % (key, str(exc)[:120]))

json.dump(ids, open(JSON_PATH, "w"), indent=2)
print("MERGED _demo_ids.json keys:", sorted(ids))
