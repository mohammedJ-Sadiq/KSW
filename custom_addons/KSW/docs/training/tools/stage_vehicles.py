"""
Stage Vehicle Allocation / Custody (HR-12) / Employee Transfers / Documents
demo data in the DEV database for the "vehicles & staff" handbooks.

    python odoo-bin shell -c KSW_dev.conf --http-port=18071 < \
        custom_addons/KSW/docs/training/tools/stage_vehicles.py

Creates (once; re-running reuses what exists):
  - departments "Training Distribution" (managed by Train Supervisor) and
    "Training Route Sales" (a second department that asks for vehicles),
    both vehicle-holding, home workshop Nabiya;
  - Isuzu trucks TRN-IS1..TRN-IS6, company vehicles of Training Distribution,
    each with valid handover papers (istimara, insurance, inspection, tank
    licence) so the handover checks pass;
  - drivers: Ahmed (TRN-IS1), Bilal (TRN-IS2), Karim (TRN-IS3, released),
    Sami and Omar (licensed, no vehicle), Yasir (no licence);
  - a login train.workshop (Workshop Manager, Nabiya);
  - roles: train.supervisor = vehicle department user + transfer requester;
    train.gm = vehicle and transfer GM; train.hr = custody HR + transfer HR.

Records, one per state the handbooks show (ids in _demo_ids.json, veh_*):
  veh_release        vehicle release TRN-IS4, waiting for the GM
  veh_addition       Training Route Sales asks for one Isuzu, waiting for the GM
  veh_handin         TRN-IS5 released and sent to the workshop by the GM (HR-12 hand-in)
  veh_dc_done        driver change TRN-IS1 Ahmed -> Sami, done
  veh_dc_draft       driver change TRN-IS2 Bilal -> Yasir, draft (no licence: refused)
  veh_doc_request    Yasir's licence requested from HR
  veh_emp_release    Karim released, his TRN-IS3 planned for Omar, waiting for the GM
  veh_emp_addition   Training Route Sales asks for one driver, waiting for the GM

Dev only.
"""
import json
import os
from datetime import date, timedelta

ids_path = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
ids = json.load(open(ids_path)) if os.path.exists(ids_path) else {}

Users = env["res.users"]
sup_user = Users.search([("login", "=", "train.supervisor")], limit=1)
gm_user = Users.search([("login", "=", "train.gm")], limit=1)
hr_user = Users.search([("login", "=", "train.hr")], limit=1)
assert sup_user and gm_user and hr_user, "run setup_demo_data.py first"
admin = env.ref("base.user_admin")
nabiya = env.ref("KSW_workshop.workshop_nabiya")

# -- the workshop manager login ---------------------------------------------
ws_user = Users.with_context(active_test=False).search([("login", "=", "train.workshop")], limit=1)
if not ws_user:
    ws_user = Users.with_context(no_reset_password=True).create({
        "name": "Train Workshop", "login": "train.workshop", "email": "train.workshop@example.com",
        "password": "trainKSW#2026",
        "group_ids": [(6, 0, [env.ref("base.group_user").id, env.ref("KSW_workshop.group_workshop_manager").id])],
    })
    env["hr.employee"].create({"name": "Train Workshop", "user_id": ws_user.id})
ws_user.write({"active": True, "x_workshop_ids": [(6, 0, nabiya.ids)]})

# -- departments --------------------------------------------------------------
Department = env["hr.department"]
dept_a = Department.search([("name", "=", "Training Distribution")], limit=1) or Department.create(
    {"name": "Training Distribution"})
dept_b = Department.search([("name", "=", "Training Route Sales")], limit=1) or Department.create(
    {"name": "Training Route Sales"})
route_sup = env["hr.employee"].search([("name", "=", "Training Route Supervisor")], limit=1) or \
    env["hr.employee"].create({"name": "Training Route Supervisor", "department_id": dept_b.id})
dept_a.write({"x_fleet_holder": True, "manager_id": sup_user.employee_id.id, "x_workshop_id": nabiya.id})
dept_b.write({"x_fleet_holder": True, "manager_id": route_sup.id, "x_workshop_id": nabiya.id})
sup_user.employee_id.write({"department_id": dept_a.id})

# -- roles ------------------------------------------------------------------------
def grant(user, *xmlids):
    user.write({"group_ids": [(4, env.ref(x).id) for x in xmlids]})

grant(sup_user, "KSW_fleet_allocation.group_fleet_allocation_user", "KSW_employee_transfer.group_transfer_requester",
      "KSW_workshop.group_workshop_requester")
sup_user.write({"x_fleet_department_ids": [(6, 0, dept_a.ids)]})
grant(gm_user, "KSW_fleet_allocation.group_fleet_allocation_gm", "KSW_employee_transfer.group_transfer_gm",
      "KSW_workshop.group_workshop_reviewer")
grant(hr_user, "KSW_vehicle_custody.group_custody_hr", "KSW_employee_transfer.group_transfer_hr")

# -- drivers ----------------------------------------------------------------------
Employee = env["hr.employee"]
licence_type = env.ref("KSW_documents.type_driving_licence")


def employee(name, licensed=True):
    emp = Employee.search([("name", "=", name)], limit=1) or Employee.create(
        {"name": name, "department_id": dept_a.id, "parent_id": sup_user.employee_id.id})
    if licensed and not env["ksw.vehicle.custody.request"]._ksw_register_licence(emp):
        env["ksw.document"]._record_from_source(
            licence_type, emp, "absher", number="29%08d" % emp.id, expiry=date.today() + timedelta(days=700),
            verified=True, license_class="نقل ثقيل")
    return emp


ahmed = employee("Training Driver Ahmed")
bilal = employee("Training Driver Bilal")
karim = employee("Training Driver Karim")
sami = employee("Training Driver Sami")
omar = employee("Training Driver Omar")
yasir = employee("Training Driver Yasir", licensed=False)
hamza = employee("Training Driver Hamza", licensed=False)

# -- vehicles with their papers -----------------------------------------------------
Vehicle = env["ksw.fleet.vehicle"]


def truck(name, custodian=None):
    v = Vehicle.search([("name", "=", name)], limit=1)
    if not v:
        v = Vehicle.create({"name": name, "vehicle_type": "isuzu", "x_department_id": dept_a.id})
        v.sudo().write({"client_id": env.company.partner_id.id, "state": "confirmed",
                        "x_registration_type": "private"})
    for doc_type, _status in v._ksw_handover_gaps().get(v.id, []):
        env["ksw.document"]._record_from_source(doc_type, v, "absher", number="P%s-%s" % (v.id, doc_type.id),
                                                expiry=date.today() + timedelta(days=300), verified=True)
    if custodian and v.x_custodian_id != custodian and not v.x_custody_ids:
        v._ksw_set_custodian(custodian, source="Training")
    return v


is1, is2, is3 = truck("TRN-IS1", ahmed), truck("TRN-IS2", bilal), truck("TRN-IS3", karim)
is4, is5, is6 = truck("TRN-IS4"), truck("TRN-IS5"), truck("TRN-IS6")

FleetRequest = env["ksw.fleet.request"]
Custody = env["ksw.vehicle.custody.request"]
EmpRequest = env["ksw.employee.request"]


def keep(key, record):
    ids[key] = record.id
    return record


def existing(key, model):
    rec = env[model].browse(ids.get(key) or []).exists()
    return rec


# 1. a vehicle release waiting for the GM
if not existing("veh_release", "ksw.fleet.request"):
    r = FleetRequest.with_user(sup_user).create({
        "request_type": "release", "department_id": dept_a.id, "reason": "Route to Jubail closed",
        "release_line_ids": [(0, 0, {"vehicle_id": is4.id, "note": "No more deliveries on this route"})]})
    r.with_user(sup_user).action_submit()
    keep("veh_release", r)

# 2. another department asks for an Isuzu
if not existing("veh_addition", "ksw.fleet.request"):
    r = FleetRequest.sudo().create({
        "request_type": "addition", "department_id": dept_b.id, "reason": "New school contract",
        "addition_line_ids": [(0, 0, {"vehicle_type": "isuzu", "quantity": 1, "need": "vehicle",
                                      "note": "Daily deliveries to three schools"})]})
    r.sudo().action_submit()
    keep("veh_addition", r)

# 3. a released vehicle sent to the workshop (HR-12 hand-in)
if not existing("veh_handin", "ksw.vehicle.custody.request"):
    r = FleetRequest.with_user(sup_user).create({
        "request_type": "release", "department_id": dept_a.id, "reason": "Engine needs overhaul",
        "release_line_ids": [(0, 0, {"vehicle_id": is5.id, "note": "Not reliable any more"})]})
    r.with_user(sup_user).action_submit()
    env["ksw.fleet.release.decision.wizard"].with_user(gm_user).create({
        "release_line_ids": [(6, 0, r.release_line_ids.ids)], "decision": "holding",
        "note": "Workshop to assess"}).action_confirm()
    hand_in = r.sudo().x_custody_request_ids[:1]
    keep("veh_handin", hand_in)
    ids["veh_handin_request"] = r.id

# 4. a driver change, done
if not existing("veh_dc_done", "ksw.vehicle.custody.request"):
    f = Custody.with_user(sup_user).create({"kind": "driver_change", "vehicle_id": is1.id,
                                            "new_employee_id": sami.id, "change_reason": "vacation",
                                            "change_reason_note": "Ahmed on annual leave"})
    f.with_user(sup_user).action_submit()
    keep("veh_dc_done", f)

# 5. a driver change in draft whose new driver has no licence
if not existing("veh_dc_draft", "ksw.vehicle.custody.request"):
    f = Custody.with_user(sup_user).create({"kind": "driver_change", "vehicle_id": is2.id,
                                            "new_employee_id": yasir.id, "change_reason": "suspension"})
    keep("veh_dc_draft", f)

# 5b. a draft whose new driver has no licence and nothing requested yet:
#     its Confirm shows the "Request Them from HR" button
if not existing("veh_dc_nolicence", "ksw.vehicle.custody.request"):
    f = Custody.with_user(sup_user).create({"kind": "driver_change", "vehicle_id": is6.id,
                                            "new_employee_id": hamza.id, "change_reason": "other",
                                            "change_reason_note": "Route reassigned"})
    keep("veh_dc_nolicence", f)

# 6. Yasir's licence requested from HR
dr = env["ksw.document.request"]._ksw_open_for(yasir, licence_type)
if not dr:
    dr = Custody._ksw_request_documents(yasir, is2, "New driver of TRN-IS2",
                                       existing("veh_dc_draft", "ksw.vehicle.custody.request").name)
    dr = dr.filtered(lambda r: r.employee_id == yasir)[:1]
    dr.sudo().write({"requested_by_id": sup_user.id})
keep("veh_doc_request", dr)

# 7. an employee released, his vehicle planned for Omar
if not existing("veh_emp_release", "ksw.employee.request"):
    r = EmpRequest.with_user(sup_user).create({
        "request_type": "release", "department_id": dept_a.id, "reason": "Fewer deliveries this season",
        "release_line_ids": [(0, 0, {"employee_id": karim.id, "note": "Experienced, can drive heavy"})]})
    r.x_vehicle_ids.with_user(sup_user).write({"disposition": "driver_change", "new_driver_id": omar.id})
    r.with_user(sup_user).action_submit()
    keep("veh_emp_release", r)

# 8. another department asks for a driver
if not existing("veh_emp_addition", "ksw.employee.request"):
    job = env["hr.job"].search([("name", "=", "Driver")], limit=1) or env["hr.job"].create({"name": "Driver"})
    r = EmpRequest.sudo().create({
        "request_type": "addition", "department_id": dept_b.id, "reason": "New school contract",
        "addition_line_ids": [(0, 0, {"job_id": job.id, "quantity": 1})]})
    r.sudo().action_submit()
    keep("veh_emp_addition", r)

json.dump(ids, open(ids_path, "w"), indent=1)
env.cr.commit()
print("STAGED", {k: v for k, v in ids.items() if k.startswith("veh_")})
