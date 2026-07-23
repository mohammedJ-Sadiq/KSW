"""DEV-only: hide the BAS Sync app menu from non-admins, give the demo employee
attendance self-access, and create a few attendance records so the employee's
Attendance page shows real punch records."""
emp = env["hr.employee"].search([("name", "=", "Train Employee")], limit=1)

# 1) hide BAS Sync menu from regular users (internal-only)
try:
    menu = env.ref("KSW_ext_sync.menu_bas_root", raise_if_not_found=False)
    sysg = env.ref("base.group_system")
    if menu:
        menu.sudo().write({"group_ids": [(6, 0, [sysg.id])]})
        env.cr.commit()
        print("BAS Sync menu restricted to Settings/Admin")
except Exception as e:
    print("bas hide skipped:", e)

# 2) grant the employee attendance self-access (adds the Attendances app + scopes to own)
try:
    u = env["res.users"].search([("login", "=", "train.employee")], limit=1)
    g = env.ref("KSW_base_security.group_hr_attendance_employee_subordinate",
                raise_if_not_found=False)
    if u and g:
        u.sudo().write({"group_ids": [(4, g.id)]})
        env.cr.commit()
        print("granted attendance self group to train.employee")
except Exception as e:
    print("att group skipped:", e)

# 3) create a handful of attendance records (times in UTC; +3 = local)
try:
    A = env["hr.attendance"]
    recs = [
        ("2026-07-06 05:00:00", "2026-07-06 13:00:00"),  # normal
        ("2026-07-07 05:00:00", "2026-07-07 13:00:00"),  # normal
        ("2026-07-08 05:45:00", "2026-07-08 13:00:00"),  # late in
        ("2026-07-09 05:00:00", "2026-07-09 11:30:00"),  # early out
        ("2026-07-13 05:00:00", "2026-07-13 13:00:00"),  # normal
    ]
    made = 0
    for ci, co in recs:
        try:
            if not A.search([("employee_id", "=", emp.id), ("check_in", "=", ci)], limit=1):
                A.sudo().create({"employee_id": emp.id, "check_in": ci, "check_out": co})
                env.cr.commit()
                made += 1
        except Exception as e:
            env.cr.rollback()
            print("  skip", ci, "->", str(e)[:80])
    print("attendance records created:", made,
          "total:", A.search_count([("employee_id", "=", emp.id)]))
except Exception as e:
    env.cr.rollback(); print("attendance records skipped:", str(e)[:140])
