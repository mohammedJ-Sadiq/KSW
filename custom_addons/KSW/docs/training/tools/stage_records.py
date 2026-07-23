"""
Stage sample records (loans in each approval state, a commission sheet) in the
DEV database and write their ids to tools/_demo_ids.json so capture_deep.mjs can
open the exact record for each deep screenshot.

Run after setup_demo_data.py:

    python odoo-bin shell -c KSW_dev.conf --no-http < \
        custom_addons/KSW/docs/training/tools/stage_records.py

Idempotent: loans are keyed by a distinct marker amount per state.
DEV-only sample data; safe to delete (see setup_demo_data.py).
"""
import json
import logging
import os

_logger = logging.getLogger("ksw.training.stage")

emp = env["hr.employee"].search([("name", "=", "Train Employee")], limit=1)
loan_type = env["ksw.deduction.type"].search([("is_loan", "=", True)], limit=1)
ids = {}

if not emp or not loan_type:
    print("STAGE: missing Train Employee or loan type — run setup_demo_data.py first")
else:
    Ded = env["ksw.deduction"]

    def stage_loan(key, amount, approval_state, state=None):
        rec = Ded.search([("employee_id", "=", emp.id),
                          ("amount", "=", amount)], limit=1)
        if not rec:
            rec = Ded.create({
                "employee_id": emp.id, "type_id": loan_type.id,
                "amount": amount, "installments": 10,
            })
        # generate installments + enter the workflow (best-effort)
        try:
            if hasattr(rec, "action_submit") and not rec.line_ids:
                rec.action_submit()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("stage: submit %s failed: %s", key, exc)
        vals = {"approval_state": approval_state}
        if state:
            vals["state"] = state
        rec.sudo().write(vals)
        ids[key] = rec.id
        return rec

    plan = [
        ("loan_pending_dm", 5010, "pending_dm", None),
        ("loan_pending_hr", 5020, "pending_hr", None),
        ("loan_pending_acc", 5030, "pending_acc", None),
        ("loan_pending_gm", 5040, "pending_gm", None),
        ("loan_pending_disbursement", 5050, "pending_disbursement", None),
        ("loan_active", 5060, "approved", "active"),
    ]
    for key, amt, appr, st in plan:
        try:
            stage_loan(key, amt, appr, st)
            env.cr.commit()
            print("STAGE loan %s -> id %s (%s)" % (key, ids.get(key), appr))
        except Exception as exc:  # noqa: BLE001
            env.cr.rollback()
            print("STAGE loan %s SKIPPED: %s" % (key, exc))

    # A draft commission sheet with one line for the form screenshots
    try:
        Sheet = env["ksw.commission.sheet"]
        sheet = Sheet.search([("employee_id", "=", emp.id)], limit=1)
        if not sheet:
            sheet = Sheet.create({"employee_id": emp.id})
        ids["commission_sheet"] = sheet.id
        env.cr.commit()
        print("STAGE commission sheet -> id %s" % sheet.id)
    except Exception as exc:  # noqa: BLE001
        env.cr.rollback()
        print("STAGE commission sheet SKIPPED: %s" % exc)

# write ids for the capture script
out = os.path.join(os.path.dirname(__file__) if "__file__" in dir() else
                   "custom_addons/KSW/docs/training/tools", "_demo_ids.json")
try:
    with open("custom_addons/KSW/docs/training/tools/_demo_ids.json", "w") as fh:
        json.dump(ids, fh, indent=2)
    print("WROTE _demo_ids.json:", ids)
except Exception as exc:  # noqa: BLE001
    print("could not write json:", exc, "->", ids)
