"""
Stage KSW_helpdesk demo data in the DEV database for the training screenshots.

    python odoo-bin shell -c KSW_dev.conf --http-port=18071 < \
        custom_addons/KSW/docs/training/tools/stage_helpdesk.py

Creates (idempotently):
  - the train.it login (IT Team role) + its employee
  - a small IT asset fleet: assigned, available, in maintenance,
    warranty expiring / expired
  - tickets covering every stage, both kinds, priorities, an overdue one,
    a blocked one, and closed ones with feedback (so Reporting/History are
    not empty)

Dev only. Remove with: unlink res.users like 'train.%', helpdesk.ticket and
it.asset records created here.
"""
import json
import logging
import os
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger("ksw.training.helpdesk")
PASSWORD = "trainKSW#2026"   # dev-only, same as setup_demo_data.py

ids = {}

# ---- 1) IT Team user ----------------------------------------------------
groups = [env.ref(x).id for x in
          ("base.group_user", "KSW_helpdesk.group_helpdesk_agent")]
vals = {"name": "Train It", "login": "train.it", "password": PASSWORD,
        "group_ids": [(6, 0, groups)]}
it_user = env["res.users"].with_context(no_reset_password=True).search(
    [("login", "=", "train.it")], limit=1)
if it_user:
    it_user.write(vals)
else:
    it_user = env["res.users"].with_context(no_reset_password=True).create(vals)

Emp = env["hr.employee"]


def ensure_emp(name, user=None, parent=None, **extra):
    e = Emp.search([("name", "=", name)], limit=1)
    v = dict(extra)
    if user:
        v["user_id"] = user.id
    if parent:
        v["parent_id"] = parent.id
    if e:
        e.write(v)
    else:
        e = Emp.create(dict(v, name=name))
    return e


it_emp = ensure_emp("Train It", it_user, job_title="IT Support Specialist")
sup_emp = Emp.search([("name", "=", "Train Supervisor")], limit=1)
emp_emp = Emp.search([("name", "=", "Train Employee")], limit=1)

# contact details so the ticket Caller card is not empty
emp_emp.write({"work_email": "train.employee@alkawthersw.com",
               "work_phone": "+966 11 000 0000",
               "mobile_phone": "+966 55 000 0000",
               "job_title": emp_emp.job_title or "Sales Representative"})
env.cr.commit()
ids["it_user"] = it_user.id
print("IT USER:", it_user.id)

# ---- 2) assets ----------------------------------------------------------
Asset = env["it.asset"]
today = fields.Date.context_today(env["res.users"])


def cat(name):
    return env["it.asset.category"].search([("name", "=", name)], limit=1)


def ensure_asset(name, category, **vals):
    a = Asset.with_context(active_test=False).search([("name", "=", name)], limit=1)
    vals = dict(vals, name=name, category_id=cat(category).id)
    if a:
        a.write(vals)
    else:
        a = Asset.create(vals)
    return a


laptop = ensure_asset(
    "Dell Latitude 5420 Laptop", "Laptop", brand="Dell", model_name="Latitude 5420",
    serial_number="DL5420-KSW-0142", location="HQ - 3rd Floor",
    purchase_date=today - timedelta(days=400), purchase_value=4800.0,
    warranty_expiry_date=today + timedelta(days=330))
printer = ensure_asset(
    "HP LaserJet Pro M404dn", "Printer", brand="HP", model_name="LaserJet Pro M404dn",
    serial_number="HPM404-KSW-0007", location="HQ - 2nd Floor Print Room",
    purchase_date=today - timedelta(days=700), purchase_value=1750.0,
    warranty_expiry_date=today + timedelta(days=18))       # expiring soon
monitor = ensure_asset(
    "Dell P2422H 24\" Monitor", "Monitor", brand="Dell", model_name="P2422H",
    serial_number="DLP2422-KSW-0311", location="HQ - IT Store",
    purchase_date=today - timedelta(days=200), purchase_value=750.0,
    warranty_expiry_date=today + timedelta(days=530))
phone = ensure_asset(
    "Samsung Galaxy A54", "Mobile Phone", brand="Samsung", model_name="Galaxy A54",
    serial_number="356938035643809", location="HQ - IT Store",
    purchase_date=today - timedelta(days=150), purchase_value=1600.0,
    warranty_expiry_date=today + timedelta(days=580))
thinkpad = ensure_asset(
    "Lenovo ThinkPad T14 Laptop", "Laptop", brand="Lenovo", model_name="ThinkPad T14",
    serial_number="LNT14-KSW-0088", location="HQ - IT Store",
    purchase_date=today - timedelta(days=900), purchase_value=5200.0,
    warranty_expiry_date=today + timedelta(days=120))
switch = ensure_asset(
    "Cisco Catalyst 2960 Switch", "Networking Equipment", brand="Cisco",
    model_name="Catalyst 2960-24TT-L", serial_number="FOC1712X0KL",
    location="HQ - Server Room", purchase_date=today - timedelta(days=1500),
    purchase_value=3400.0,
    warranty_expiry_date=today - timedelta(days=60))       # expired


def ensure_assigned(asset, employee, days_ago, condition="good"):
    """Put the asset in an employee's custody (mirrors the assign wizard)."""
    if asset.state == "assigned" and asset.employee_id == employee:
        return
    if not env["it.asset.assignment"].search([
            ("asset_id", "=", asset.id), ("employee_id", "=", employee.id),
            ("state", "=", "active")]):
        env["it.asset.assignment"].create({
            "asset_id": asset.id, "employee_id": employee.id,
            "date_assigned": today - timedelta(days=days_ago),
            "condition_out": condition,
            "notes": "Handed over with charger and carry case.",
        })
    asset.write({"state": "assigned", "employee_id": employee.id})


ensure_assigned(laptop, emp_emp, 380)
if sup_emp:
    ensure_assigned(phone, sup_emp, 120)

# one returned assignment, so the history shows both sides
if not env["it.asset.assignment"].search([("asset_id", "=", monitor.id)]):
    env["it.asset.assignment"].create({
        "asset_id": monitor.id, "employee_id": emp_emp.id,
        "date_assigned": today - timedelta(days=180),
        "date_returned": today - timedelta(days=30),
        "condition_out": "new", "condition_in": "good", "state": "returned",
        "notes": "Returned on desk move.",
    })

# one asset out for repair
if thinkpad.state != "maintenance":
    thinkpad.write({"state": "available", "employee_id": False})
    if not env["it.asset.maintenance"].search([
            ("asset_id", "=", thinkpad.id), ("state", "=", "in_progress")]):
        env["it.asset.maintenance"].create({
            "asset_id": thinkpad.id,
            "issue": "Keyboard not responding - sent to vendor",
            "date_start": today - timedelta(days=6),
            "cost": 450.0,
        })
    thinkpad.write({"state": "maintenance"})

env.cr.commit()
ids.update({"asset_laptop": laptop.id, "asset_printer": printer.id,
            "asset_monitor": monitor.id, "asset_phone": phone.id,
            "asset_maintenance": thinkpad.id, "asset_switch": switch.id})
print("ASSETS:", {a.asset_tag: a.state for a in
                 (laptop, printer, monitor, phone, thinkpad, switch)})

# ---- 3) tickets ---------------------------------------------------------
Ticket = env["helpdesk.ticket"]
stage = {s.name: s for s in env["helpdesk.ticket.stage"].search([])}


def tcat(name, ttype):
    return env["helpdesk.ticket.category"].search(
        [("name", "=", name), ("ticket_type", "=", ttype)], limit=1)


def ensure_ticket(key, name, ttype, category, employee, **vals):
    t = Ticket.search([("name", "=", name)], limit=1)
    vals = dict(vals, name=name, ticket_type=ttype, employee_id=employee.id,
                category_id=tcat(category, ttype).id)
    if t:
        t.write(vals)
    else:
        t = Ticket.create(vals)
    ids[key] = t.id
    return t


t_new = ensure_ticket(
    "ticket_new", "Laptop won't turn on", "incident", "Hardware", emp_emp,
    description="<p>The laptop does not power on at all since this morning. "
                "The charger LED is on, but pressing the power button does "
                "nothing - no fan, no screen. I tried a different power "
                "socket and holding the power button for 30 seconds.</p>",
    priority="2", stage_id=stage["New"].id, asset_id=laptop.id,
    deadline=today + timedelta(days=2))

t_progress = ensure_ticket(
    "ticket_in_progress", "No internet in the 3rd floor meeting room", "incident",
    "Network & Internet", sup_emp or emp_emp,
    description="<p>Wi-Fi shows connected but no pages load in the 3rd floor "
                "meeting room. Works normally at the desks outside. Started "
                "after yesterday's power cut.</p>",
    priority="3", stage_id=stage["In Progress"].id, user_id=it_user.id,
    kanban_state="normal", deadline=today + timedelta(days=1))

t_blocked = ensure_ticket(
    "ticket_blocked", "Printer jams on double-sided printing", "incident",
    "Printer", emp_emp,
    description="<p>Every double-sided job jams in the rear tray. Single-sided "
                "printing works. Roughly 1 in 2 jobs.</p>",
    priority="1", stage_id=stage["On Hold"].id, user_id=it_user.id,
    kanban_state="blocked", asset_id=False)

t_overdue = ensure_ticket(
    "ticket_overdue", "Second monitor not detected on the docking station",
    "incident", "Hardware", emp_emp,
    description="<p>The docking station only drives one of the two monitors "
                "since the last Windows update.</p>",
    priority="1", stage_id=stage["In Progress"].id, user_id=it_user.id,
    deadline=today - timedelta(days=3))

t_req = ensure_ticket(
    "ticket_request", "Install AutoCAD on my workstation", "request",
    "New Software Installation", emp_emp,
    description="<p>I need AutoCAD 2024 for the new project drawings. "
                "The license was approved by my manager last week.</p>",
    priority="1", stage_id=stage["New"].id)

t_req2 = ensure_ticket(
    "ticket_request_access", "Access to the shared Finance folder", "request",
    "Access / Permission Request", sup_emp or emp_emp,
    description="<p>Please grant read access to \\\\fileserver\\Finance\\Reports "
                "for the monthly closing.</p>",
    priority="1", stage_id=stage["In Progress"].id, user_id=it_user.id)

closed_specs = [
    ("ticket_closed", "Outlook not sending emails", "incident", "Email",
     "great", 26, 4.5,
     "<p>Emails stay in the Outbox and are never sent. Receiving works.</p>"),
    ("ticket_closed_2", "Password reset for the ERP account", "request",
     "Password Reset", "great", 12, 0.75,
     "<p>Locked out after too many attempts. Please reset.</p>"),
    ("ticket_closed_3", "Replace faulty keyboard", "incident", "Hardware",
     "okay", 40, 22.0,
     "<p>Several keys stopped responding. Keyboard replaced from stock.</p>"),
    ("ticket_closed_4", "New employee laptop setup", "request",
     "New Hardware Request", "great", 55, 48.0,
     "<p>Laptop, email account and VPN access for the new sales hire.</p>"),
]
for key, name, ttype, category, sat, days_ago, hours, desc in closed_specs:
    t = ensure_ticket(key, name, ttype, category, emp_emp, description=desc,
                      priority="1", user_id=it_user.id)
    created = fields.Datetime.now() - timedelta(days=days_ago)
    t.write({"stage_id": stage["Closed"].id, "kanban_state": "done",
             "close_date": created + timedelta(hours=hours),
             "closed_by": it_user.id, "satisfaction": sat})
    # create_date is a magic column - set it in SQL so Resolution Time is real
    env.cr.execute("UPDATE helpdesk_ticket SET create_date = %s WHERE id = %s",
                   (created, t.id))
closed = Ticket.browse([ids[spec[0]] for spec in closed_specs])
closed.invalidate_recordset()
closed._compute_resolution_hours()

# a little conversation so the chatter is not empty in the screenshots
for ticket, body in (
    (t_progress, "Checked the access point in the meeting room - it is up but "
                 "not passing DHCP. Escalated to the network vendor."),
    (t_blocked, "Waiting for the replacement rear roller kit from the supplier "
                "(ETA 3 working days)."),
):
    if not ticket.message_ids.filtered(lambda m: m.body and body[:30] in (m.body or "")):
        ticket.message_post(body=body, subtype_xmlid="mail.mt_comment")

env.cr.commit()
print("TICKETS:", {k: v for k, v in ids.items() if k.startswith("ticket")})

# ---- 4) merge ids into _demo_ids.json -----------------------------------
path = "custom_addons/KSW/docs/training/tools/_demo_ids.json"
try:
    with open(path) as fh:
        existing = json.load(fh)
except Exception:
    existing = {}
existing.update(ids)
with open(path, "w") as fh:
    json.dump(existing, fh, indent=2, sort_keys=False)
print("IDS WRITTEN:", path)
print("HELPDESK STAGING COMPLETE")
