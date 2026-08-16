"""One-off import of the legacy Google Sheet "Workshop" requests into
ksw.workshop.request / ksw.fleet.vehicle.

This is NOT an Odoo migration step (it doesn't belong in migrations/ and
does not run automatically on -u) — it is meant to be run once, by hand,
after exporting the Google Sheet to CSV.

Usage (from an interactive `odoo-bin shell -c KSW_dev.conf --no-http`):

    >>> exec(open('custom_addons/KSW/KSW_workshop/scripts/import_history.py').read())
    >>> report = import_workshop_history(env, '/path/to/export.csv')  # dry run
    >>> print_report(report)
    >>> report = import_workshop_history(env, '/path/to/export.csv', commit=True)
    >>> env.cr.commit()

Always run the dry run first (commit=False, the default) and review
`report['skipped']` / `report['flagged']` before committing — this data is
messy free text (typos, inconsistent driver/vehicle naming) and the script
deliberately refuses to guess: anything it can't confidently resolve is
flagged for manual review rather than silently imported wrong.
"""
import csv
import re
from datetime import datetime

STATE_MAP = {
    'جديد': 'new',
    'قيد العمل': 'in_progress',
    'مكتمل': 'completed',
    'مرفوض': 'rejected',
}

WORKSHOP_LABEL = 'الورشة (Workshop)'

VEHICLE_CODE_RE = re.compile(r'\d+')


def _parse_date(value):
    value = (value or '').strip()
    if not value:
        return None
    for fmt in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_vehicle_code(subject_text, vehicle_code_col):
    """Best-effort fleet number: prefer the explicit column, else the
    leading run of digits in "المعني بالطلب" (e.g. "152 سياب" -> "152")."""
    vehicle_code_col = (vehicle_code_col or '').strip()
    if vehicle_code_col:
        return vehicle_code_col
    match = VEHICLE_CODE_RE.search(subject_text or '')
    return match.group(0) if match else None


def _find_or_create_vehicle(env, code, model_text):
    if not code:
        return None, False
    vehicle = env['ksw.fleet.vehicle'].search([('name', '=', code)], limit=1)
    if vehicle:
        return vehicle, False
    vehicle = env['ksw.fleet.vehicle'].create({
        'name': code,
        'vehicle_model': (model_text or '').strip() or False,
    })
    return vehicle, True


def _find_employee_by_email(env, email):
    email = (email or '').strip().lower()
    if not email:
        return None
    return env['hr.employee'].search([('work_email', '=ilike', email)], limit=1)


def _find_employee_by_name(env, name):
    """Best-effort exact-name match. Deliberately does NOT fuzzy-match —
    a wrong technician/driver attribution is worse than a blank field."""
    name = (name or '').strip()
    if not name:
        return None
    matches = env['hr.employee'].search([('name', '=ilike', name)])
    return matches[0] if len(matches) == 1 else None


def import_workshop_history(env, csv_path, commit=False):
    """Returns a report dict: {'created': [...], 'skipped': [...], 'flagged': [...]}.

    - 'skipped': rows outside scope (not a Workshop request) or already imported
      (x_legacy_uid already present) — never re-created.
    - 'flagged': rows that WERE created but with something unresolved (unmatched
      employee/driver/technician, unparseable vehicle) — review these manually.
    """
    report = {'created': [], 'skipped': [], 'flagged': []}
    Request = env['ksw.workshop.request']

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        for row_num, row in enumerate(csv.DictReader(f), start=2):
            uid = (row.get('UID_HEADER') or '').strip()

            if (row.get('الجهة المعنية') or '').strip() != WORKSHOP_LABEL:
                report['skipped'].append((row_num, uid, 'not a Workshop request'))
                continue

            if uid and Request.search([('x_legacy_uid', '=', uid)], limit=1):
                report['skipped'].append((row_num, uid, 'already imported'))
                continue

            flags = []

            employee = _find_employee_by_email(env, row.get('Email Address'))
            if not employee:
                report['skipped'].append((
                    row_num, uid,
                    f"no employee matched for email {row.get('Email Address')!r} — "
                    f"import manually once the requester's Odoo account is known",
                ))
                continue

            code = _parse_vehicle_code(row.get('المعني بالطلب'), row.get('كود السيارة'))
            if not code:
                flags.append(f"could not parse a vehicle code from {row.get('المعني بالطلب')!r}")
                continue  # vehicle_id is required; unparseable rows can't be created at all
            vehicle, vehicle_created = _find_or_create_vehicle(env, code, row.get('المعني بالطلب'))
            if vehicle_created:
                flags.append(f"created new vehicle {code!r} — please review/complete its details")

            plate = (row.get('رقم اللوحة') or '').strip()
            if plate and not vehicle.plate_number:
                vehicle.plate_number = plate

            driver = _find_employee_by_name(env, row.get('اسم السائق'))
            if (row.get('اسم السائق') or '').strip() and not driver:
                flags.append(f"driver name {row.get('اسم السائق')!r} did not match exactly one employee")

            technician = _find_employee_by_name(env, row.get('الفني المسؤول لإصلاح المركبة'))
            if (row.get('الفني المسؤول لإصلاح المركبة') or '').strip() and not technician:
                flags.append(f"technician name {row.get('الفني المسؤول لإصلاح المركبة')!r} did not match exactly one employee")

            state = STATE_MAP.get((row.get('حالة الطلب') or '').strip(), 'new')

            request_date = _parse_date(row.get('date'))
            entry_dt = _parse_date(f"{row.get('تاريخ الدخول', '')} {row.get('وقت الدخول', '')}".strip())
            exit_dt = _parse_date(f"{row.get('تاريخ الخروج', '')} {row.get('وقت الخروج', '')}".strip())
            completion_dt = _parse_date(row.get('تاريخ اكتمال الطلب'))

            def _float(value):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0

            def _int(value):
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    return 0

            vals = {
                'employee_id': employee.id,
                'vehicle_id': vehicle.id,
                'driver_id': driver.id if driver else False,
                'description': (row.get('نوع الطلب') or '').strip() or 'Imported request',
                'state': state,
                'note': (row.get('ملاحظة') or '').strip() or False,
                'note_to_requester': (row.get('ملاحظة لمقدم الطلب') or '').strip() or False,
                'completion_date': completion_dt,
                'entry_datetime': entry_dt,
                'exit_datetime': exit_dt,
                'odometer_reading': _int(row.get('رقم العداد')),
                'tire_pressure': (row.get('ضغط الكفرات') or '').strip() or False,
                'tire_bolts': (row.get('مسامير الكفرات') or '').strip() or False,
                'work_statement': (row.get('بيان المطلوب') or '').strip() or False,
                'repairs_parts': (row.get('الإصلاحات وقطع الغيار') or '').strip() or False,
                'technician_id': technician.id if technician else False,
                'parts_cost': _float(row.get('تكاليف قطع الغيار')),
                'labor_cost': _float(row.get('تكاليف أجور اليد(إن وجدت)')),
                'x_legacy_uid': uid or False,
                'x_imported': True,
            }

            try:
                with env.cr.savepoint():
                    request = Request.with_context(
                        mail_notrack=True, mail_create_nolog=True,
                    ).sudo().create(vals)
                    if request_date:
                        env.cr.execute(
                            "UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s",
                            (request_date, request.id),
                        )
            except Exception as exc:  # noqa: BLE001 - a bad row must not abort the whole import
                report['skipped'].append((row_num, uid, f'create failed: {exc}'))
                continue

            if flags:
                report['flagged'].append((row_num, uid, request.id, flags))
            else:
                report['created'].append((row_num, uid, request.id))

    if commit:
        env.flush_all()
    else:
        env.cr.rollback()

    return report


def print_report(report):
    print(f"Created cleanly: {len(report['created'])}")
    print(f"Created but flagged for review: {len(report['flagged'])}")
    for row_num, uid, request_id, flags in report['flagged']:
        print(f"  row {row_num} ({uid}, id={request_id}):")
        for flag in flags:
            print(f"    - {flag}")
    print(f"Skipped: {len(report['skipped'])}")
    for row_num, uid, reason in report['skipped']:
        print(f"  row {row_num} ({uid}): {reason}")
