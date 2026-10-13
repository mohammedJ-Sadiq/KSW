"""
Stage KSW_workshop demo data in the DEV database for the training screenshots.

    python odoo-bin shell -c KSW_dev.conf --http-port=18071 < \
        custom_addons/KSW/docs/training/tools/stage_workshop.py

Since Oct 2026 only a Workshop Requester raises requests, and (with
KSW_workshop_tanker) his vehicle picker offers only the trucks in his tanker
groups plus the trucks his reporting line drives. So this script:

  - gives train.supervisor the Requester role;
  - creates the tanker group "Training Isuzu Group" with three Isuzu trucks,
    train.supervisor as its supervisor;
  - makes Train Employee (who reports to train.supervisor) the default driver
    of a fourth truck, the "driven by my team" case;
  - raises requests as train.supervisor, one per state the requester sees
    (New, In Progress, Completed with a note, Rejected with a reason), plus a
    New one whose driver differs from the truck's driver.

Manager steps run as admin (a workshop manager) so the chatter shows the
workshop, not the requester, completing or rejecting. Only trucks with no
BAS driver and no default driver are used, so no real employee is named.

KSW_DEMO_LANG=ar stages the same set in Arabic (descriptions, notes, and
the chatter, which is written in the language of whoever posts it), for the
screenshots-ar/ pass. Switching language replaces the other set; run it
again with KSW_DEMO_LANG=en before re-shooting the English screenshots.

Dev only. Remove: unlink the workshop_* request ids in _demo_ids.json, the
tanker group, and reset driver_id on the workshop_team_vehicle id.
"""
import json
import os

from odoo import fields

ids_path = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
ids = json.load(open(ids_path)) if os.path.exists(ids_path) else {}

sup_user = env["res.users"].search([("login", "=", "train.supervisor")], limit=1)
emp_user = env["res.users"].search([("login", "=", "train.employee")], limit=1)
assert sup_user.employee_id and emp_user.employee_id, "run setup_demo_data.py first"
supervisor, employee = sup_user.employee_id, emp_user.employee_id
assert employee.parent_id == supervisor, "Train Employee must report to Train Supervisor"
admin = env.ref("base.user_admin")

sup_user.write({"group_ids": [(4, env.ref("KSW_workshop.group_workshop_requester").id)]})

client = env.company.partner_id
Vehicle = env["ksw.fleet.vehicle"]
vehicles = Vehicle.browse(ids.get("workshop_vehicles", [])).exists()
if len(vehicles) != 5:
    vehicles = Vehicle.search([
        ("client_id", "=", client.id), ("vehicle_type", "=", "isuzu"),
        ("state", "=", "confirmed"), ("x_bas_driver_id", "=", False),
        ("driver_id", "=", False),
    ], order="name", limit=5)
assert len(vehicles) == 5, "need five driverless confirmed Isuzu trucks"
ids["workshop_vehicles"] = vehicles.ids
group_trucks, team_truck = vehicles[:3], vehicles[3]
spare_truck = vehicles[4]   # in no group, nobody on the team drives it

Group = env["ksw.water.tanker.group"]
group = Group.with_context(active_test=False).search(
    [("name", "=", "Training Isuzu Group")], limit=1)
vals = {"name": "Training Isuzu Group", "active": True,
        "vehicle_ids": [(6, 0, group_trucks.ids)],
        "supervisor_ids": [(6, 0, supervisor.ids)]}
if group:
    group.write(vals)
else:
    group = Group.create(vals)
ids["workshop_tanker_group"] = group.id
team_truck.driver_id = employee
ids["workshop_team_vehicle"] = team_truck.id
ids["workshop_out_of_scope_vehicle"] = spare_truck.id

LANG = os.environ.get("KSW_DEMO_LANG", "en")
CTX_LANG = "ar_001" if LANG == "ar" else "en_US"
Request = env["ksw.workshop.request"]
if ids.get("workshop_lang", "en") != LANG:
    # the other language's set: chatter cannot be re-translated, so recreate
    Request.browse([ids.pop(k) for k in list(ids)
                    if k.startswith("workshop_") and isinstance(ids[k], int)
                    and k not in ("workshop_tanker_group", "workshop_team_vehicle",
                                  "workshop_out_of_scope_vehicle")]).exists().unlink()
ids["workshop_lang"] = LANG
TEXT = {
    "en": {
        "new": "Brakes squeal when stopping and the pedal feels soft since Sunday.",
        "in_progress": "Oil change due, and the oil warning light comes on at idle.",
        "completed": "Front left tyre worn on the inner edge, please replace and check alignment.",
        "rejected": "AC not cooling.",
        "mismatch": "Water pump leaking at the rear. The usual driver is on leave, so I brought it in.",
        "done_note": "Tyre replaced and alignment done. Please collect the vehicle from bay 2.",
        "reject_note": "Duplicate of the request already raised for this vehicle this week; "
                       "follow that one instead.",
    },
    "ar": {
        "new": "صوت صرير عند الفرملة، والدواسة لينة منذ يوم الأحد.",
        "in_progress": "موعد تغيير الزيت، ولمبة الزيت تضيء والمحرك على الوضع الخامل.",
        "completed": "الإطار الأمامي الأيسر متآكل من الحافة الداخلية، يرجى تغييره وفحص الميزانية.",
        "rejected": "المكيف لا يبرد.",
        "mismatch": "تسريب من مضخة المياه في الخلف. السائق المعتاد في إجازة، لذا أحضرتها بنفسي.",
        "done_note": "تم تغيير الإطار وضبط الميزانية. يرجى استلام المركبة من البوابة 2.",
        "reject_note": "الطلب مكرر لطلب مرفوع لهذه المركبة هذا الأسبوع؛ يرجى متابعة الطلب الآخر.",
    },
}[LANG]
SPECS = [
    # key, truck, driver, description
    ("workshop_new", team_truck, employee, TEXT["new"]),
    ("workshop_in_progress", group_trucks[0], supervisor, TEXT["in_progress"]),
    ("workshop_completed", group_trucks[1], supervisor, TEXT["completed"]),
    ("workshop_rejected", group_trucks[2], supervisor, TEXT["rejected"]),
    ("workshop_mismatch", team_truck, supervisor, TEXT["mismatch"]),
]

for key, vehicle, driver, description in SPECS:
    req = Request.browse(ids.get(key)).exists()
    if req and req.employee_id != supervisor:
        req.unlink()   # an older run raised it as train.employee
        req = Request
    if not req:
        req = Request.with_user(sup_user).with_context(lang=CTX_LANG).create({
            "client_id": client.id,
            "vehicle_type": "isuzu",
            "vehicle_id": vehicle.id,
            "driver_id": driver.id,
            "description": description,
        })
    ids[key] = req.id
    req = req.with_user(admin).with_context(lang=CTX_LANG)
    if key == "workshop_in_progress" and req.state == "new":
        req.action_start()
    elif key == "workshop_completed" and req.state == "new":
        req.note_to_requester = TEXT["done_note"]
        req.action_start()
        req.write({
            "entry_datetime": fields.Datetime.now(),
            "exit_datetime": fields.Datetime.now(),
            "odometer_reading": 184250,
        })
        req.action_complete()
    elif key == "workshop_rejected" and req.state == "new":
        # The requester never sees rejection_reason (manager-only tab, and
        # the reject message carries only note_to_requester), so a manager
        # has to repeat the reason there for the requester to learn it.
        reason = TEXT["reject_note"]
        req.write({"rejection_reason": reason, "note_to_requester": reason})
        req.action_reject()
    print(key, req.name, req.state, req.vehicle_id.name, req.x_driver_mismatch)

print("group trucks", group_trucks.mapped("name"), "team truck", team_truck.name,
      "out of scope", spare_truck.name)
json.dump(ids, open(ids_path, "w"), indent=2)
env.cr.commit()
