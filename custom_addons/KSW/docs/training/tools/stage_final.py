"""Stage the last data needed for the remaining deep screenshots (DEV only):
an attendance sheet with day lines, a confirmed commission sheet, and a
skipped-employee log entry. Merges ids into _demo_ids.json."""
import json, os
JSON_PATH = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
ids = json.load(open(JSON_PATH)) if os.path.exists(JSON_PATH) else {}
emp = env["hr.employee"].search([("name", "=", "Train Employee")], limit=1)

# 1) attendance sheet with day rows
try:
    S = env["ksw.attendance.sheet"]
    sh = S.search([("employee_id", "=", emp.id)], limit=1)
    if not sh:
        sh = S.create({"employee_id": emp.id, "month": "7", "year": 2026})
    if hasattr(sh, "action_generate_lines") and not sh.line_ids:
        sh.action_generate_lines()
    ids["att_sheet"] = sh.id
    env.cr.commit()
    print("ATT SHEET id", sh.id, "lines", len(sh.line_ids))
except Exception as e:
    env.cr.rollback(); print("att sheet skipped:", str(e)[:120])

# 2) commission sheet -> add a line + confirm (so Accounting sees "Awaiting Accountant")
try:
    cs = env["ksw.commission.sheet"].browse(ids.get("commission_sheet") or 0)
    if not cs.exists():
        cs = env["ksw.commission.sheet"].search([("employee_id", "=", emp.id)], limit=1)
    if cs and not cs.line_ids:
        cs.write({"line_ids": [(0, 0, {
            "category_id": 1, "amount": 500.0})]})
    if cs and cs.state == "draft":
        try:
            cs.action_confirm()
        except Exception:
            cs.sudo().write({"state": "confirmed"})
    ids["commission_sheet"] = cs.id
    env.cr.commit()
    print("COMMISSION sheet id", cs.id, "state", cs.state)
except Exception as e:
    env.cr.rollback(); print("commission confirm skipped:", str(e)[:120])

# 3) skipped-employee log entry on a draft batch (232)
try:
    run = env["hr.payslip.run"].browse(232)
    Skip = env["ksw.payslip.run.skip.line"]
    if run.exists() and not Skip.search([("run_id", "=", 232), ("employee_id", "=", emp.id)]):
        Skip.create({"run_id": 232, "employee_id": emp.id,
                     "reason": "Unconfirmed annual-leave return"})
    ids["skip_batch"] = 232
    env.cr.commit()
    print("SKIP line added to batch 232")
except Exception as e:
    env.cr.rollback(); print("skip line skipped:", str(e)[:120])

# static references (existing real records reused for read-only shots)
ids.setdefault("payslip", 16260)         # real payslip with salary lines
ids.setdefault("comm_batch", 10)          # real commission batch
ids.setdefault("sales_sheet", 6)          # real sales commission sheet
ids.setdefault("payslip_batch", 240)      # real done batch with slips

json.dump(ids, open(JSON_PATH, "w"), indent=2)
print("MERGED:", sorted(ids))
