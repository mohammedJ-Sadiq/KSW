"""One-off import of the legacy Google Sheet "Workshop" requests into
ksw.workshop.request / ksw.fleet.vehicle.

This is NOT an Odoo migration step (it doesn't belong in migrations/ and
does not run automatically on -u) — it is meant to be run once, by hand.

Sources (both required for a full historical import):

- **Year files** (2022–2025): the original per-year exports, 41 columns,
  full schema including the repair-report block and an explicit vehicle
  code column ("كود السيارة"). These are the authoritative source for
  their years — status is always resolved (no "#N/A").
- **Master file**: "معاملات الشؤون الإدارية", the live multi-department
  tracker. Only its 2026 rows are used — everything from 2022-2025 in this
  file has a broken '#N/A' status (its formula used to pull from the
  now-split-off year sheets) and is skipped here in favour of the year
  files. Deduplicated against the year files by UID_HEADER, so a row is
  never imported twice even if it appears in both.

Usage (from an interactive `odoo-bin shell -c KSW_dev.conf --no-http`):

    >>> exec(open('custom_addons/KSW/KSW_workshop/scripts/import_history.py').read())
    >>> report = import_workshop_history(
    ...     env,
    ...     year_files=[
    ...         '/home/odoo/Odoo/معاملات الورشة 2022 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2023 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2024 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2025 - Sheet1.csv',
    ...     ],
    ...     master_file='/home/odoo/Odoo/معاملات الشؤون الإدارية - معاملات الشؤون الإدارية.csv',
    ...     commit=False,  # dry run — always review the report before commit=True
    ... )
    >>> print_report(report)
    >>> report = import_workshop_history(env, year_files=[...], master_file='...', commit=True)
    >>> env.cr.commit()

Decisions baked in per the user (2026-08-16):
- Rows whose real status is unresolvable ('#N/A' in the master file, not
  covered by a year file) are SKIPPED, not guessed.
- Vehicle matching does NOT try to disambiguate an IS-code vs a T-code
  when a bare number could be either (or matches neither/none at all) —
  it just uses the bare number as the vehicle's fleet code so the request
  still gets imported ("ignore that part of the name, but get it there").
  Rows with literally no vehicle reference anywhere link to a single
  shared placeholder vehicle, `UNSPECIFIED`.
- Requester email: `EMAIL_REMAP` below corrects the two known cases where
  the legacy form was submitted from a personal address instead of the
  employee's real one. Everyone else who submitted under an email with NO
  matching hr.employee (mostly operators who never had an Odoo account,
  confirmed by the user) gets a bare placeholder hr.employee created from
  their submitted name (no user_id — "empty odoo user"), found-or-created
  once per distinct name and reused. Either way, the exact name and email
  string from the source row is preserved on the request itself
  (`x_legacy_requester_name` / `x_legacy_requester_email`) so nothing about
  who actually submitted it is lost, even when employee_id is a
  placeholder.
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

UNSPECIFIED_VEHICLE_CODE = 'UNSPECIFIED'

# Supervisors who write a bare fleet number always mean an Isuzu, so '370'
# from them is 'IS370' (user rule, 2026-08-23). Keyed on the source sheet's
# email rather than the resolved employee: two hr.employee records share
# tareq@alkawthersw.com, so employee lookup is not a stable identifier.
IS_REQUESTER_EMAILS = {
    'tareq990040@gmail.com', 'tareq@alkawthersw.com',
    'm.disouky20@gmail.com', 'm.disouky@alkawthersw.com',
}

# Known cases where the legacy form was submitted from a personal address
# instead of the employee's real one — confirmed by the user (2026-08-16).
EMAIL_REMAP = {
    'tareq990040@gmail.com': 'tareq@alkawthersw.com',
    'm.disouky20@gmail.com': 'm.disouky@alkawthersw.com',
}

# Year-file schema (2022-2025), in column order — the 2025 export has no
# header row, so this list doubles as the fieldnames for csv.DictReader.
YEAR_FILE_FIELDNAMES = [
    'date', 'Time', 'اسم مقدم الطلب', 'قسم مقدم الطلب', 'نوع الطلب',
    'Email Address', 'رقم الجوال', 'صفة مقدم الطلب', 'المعني بالطلب',
    'المستندات والملحقات', 'الجهة المعنية', 'UID_HEADER', 'حالة الطلب',
    'ملاحظة', 'اخر تحديث للمعاملة', 'هل الطلب مغلق؟', 'ملاحظة لمقدم الطلب',
    'المدة المتوقعة للإنجاز', 'الفترة المتبقية للإنجاز',
    'الحالة المنقولة لمدير النظام', 'تاريخ اكتمال الطلب', 'مدة الإنجاز ',
    'التقارير تبدأ من هنا', 'تاريخ الدخول', 'وقت الدخول', 'رقم اللوحة',
    'كود السيارة', 'اسم السائق', 'جهة العمل', 'تاريخ الخروج', 'وقت الخروج',
    'رقم العداد', 'ضغط الكفرات', 'مسامير الكفرات', 'بيان المطلوب',
    'الإصلاحات وقطع الغيار', 'الفني المسؤول لإصلاح المركبة',
    'تكاليف قطع الغيار', 'تكاليف أجور اليد(إن وجدت)',
    'تاريخ إدخال البيانات', 'تاريخ أخر تحديث للبيانات',
]

EXPLICIT_CODE_RE = re.compile(r'^(IS|T)?\d{1,4}$')
NUMBER_RE = re.compile(r'\d+')


def _parse_date(value):
    value = (value or '').strip()
    if not value:
        return None
    # The year files write 12-hour with AM/PM ('9/12/2022 2:55:53 PM'); the
    # master file's Timestamp is 24-hour ('8/25/2022 16:38:23'). Missing the
    # second format silently returned None for every master-file row, so
    # _import_row skipped its create_date stamping and all 3,598 of the 2026
    # requests were dated the moment of import instead of when they happened.
    for fmt in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S', '%m/%d/%Y'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


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


def _is_workshop_row(destination):
    return 'ورشة' in (destination or '')


class _Caches:
    def __init__(self, env):
        self.env = env
        self.employees_by_email = {}
        self.employees_by_name = {}
        self.placeholder_employees = {}
        self.vehicles_by_code = {}
        self.unspecified_vehicle = None

    def employee_by_email(self, email):
        email = (email or '').strip().lower()
        email = EMAIL_REMAP.get(email, email)
        if not email:
            return None
        if email not in self.employees_by_email:
            # order by id so a work_email shared by two employees always
            # resolves to the same one; an unordered limit=1 silently
            # attributed 4,304 rows to a different person on dev than on
            # prod (tareq@alkawthersw.com is on two hr.employee records).
            matches = self.env['hr.employee'].search(
                [('work_email', '=ilike', email)], order='id')
            self.employees_by_email[email] = matches[0] if matches else None
        return self.employees_by_email[email]

    def employee_by_name(self, name):
        name = (name or '').strip()
        if not name:
            return None
        if name not in self.employees_by_name:
            matches = self.env['hr.employee'].search([('name', '=ilike', name)])
            self.employees_by_name[name] = matches[0] if len(matches) == 1 else None
        return self.employees_by_name[name]

    def requester_employee(self, email, name):
        """Real employee by (remapped) email if possible; otherwise a bare
        placeholder hr.employee created from the submitted name — no
        user_id, "they should be there" per the user. Returns (employee,
        created_placeholder: bool).

        Placeholder names are suffixed distinctly and matched only against
        each other (never against a bare-name search over real employees —
        many submitted names are common first names like "علي" that would
        otherwise risk attaching history to an unrelated real person)."""
        employee = self.employee_by_email(email)
        if employee:
            return employee, False

        raw_name = (name or '').strip() or (email or '').strip() or 'Unknown Requester'
        placeholder_name = f"{raw_name} (Legacy Import - No Odoo Account)"
        cache_key = placeholder_name.lower()
        if cache_key not in self.placeholder_employees:
            existing = self.env['hr.employee'].search([('name', '=', placeholder_name)], limit=1)
            employee = existing or self.env['hr.employee'].create({'name': placeholder_name})
            self.placeholder_employees[cache_key] = employee
        return self.placeholder_employees[cache_key], True

    def vehicle_by_code(self, code, vehicle_type=None):
        code = code.strip().upper()
        if code not in self.vehicles_by_code:
            vehicle = self.env['ksw.fleet.vehicle'].search([('name', '=', code)], limit=1)
            created = False
            if not vehicle:
                vals = {'name': code}
                if vehicle_type:
                    vals['vehicle_type'] = vehicle_type
                vehicle = self.env['ksw.fleet.vehicle'].create(vals)
                created = True
            elif vehicle_type and not vehicle.vehicle_type:
                vehicle.vehicle_type = vehicle_type
            self.vehicles_by_code[code] = (vehicle, created)
        return self.vehicles_by_code[code]

    def get_unspecified_vehicle(self):
        if self.unspecified_vehicle is None:
            vehicle, _created = self.vehicle_by_code(UNSPECIFIED_VEHICLE_CODE)
            self.unspecified_vehicle = vehicle
        return self.unspecified_vehicle


def _resolve_vehicle(caches, explicit_code, fallback_text, requester_email=None):
    """Returns (vehicle, flag_message_or_None)."""
    explicit_code = (explicit_code or '').strip().upper().replace(' ', '')
    if explicit_code and EXPLICIT_CODE_RE.match(explicit_code):
        vehicle, created = caches.vehicle_by_code(explicit_code)
        flag = f"created new vehicle {explicit_code!r}" if created else None
        return vehicle, flag

    match = NUMBER_RE.search(fallback_text or '')
    if match:
        num = int(match.group(0))
        is_code, t_code = f"IS{num:03d}", f"T{num:03d}"
        is_vehicle = caches.env['ksw.fleet.vehicle'].search([('name', '=', is_code)], limit=1)
        t_vehicle = caches.env['ksw.fleet.vehicle'].search([('name', '=', t_code)], limit=1)
        if bool(is_vehicle) != bool(t_vehicle):
            code = is_code if is_vehicle else t_code
            vehicle, _created = caches.vehicle_by_code(code)
            return vehicle, None
        # Ambiguous (matches both) or matches neither. A bare number is not
        # a usable fleet code, so resolve it by who submitted the row:
        #   - the Isuzu supervisors always mean an Isuzu
        #   - otherwise, when both series carry the number, it is the trailer
        # Anything else still falls back to the bare number rather than
        # inventing a vehicle in a series that does not have it.
        # More than 3 digits is not a fleet number — the real series run to
        # ~400. Those are plate numbers or odometer readings typed into the
        # vehicle field, so no prefix is invented for them.
        plausible = num <= 999
        email = (requester_email or '').strip().lower()
        if plausible and email in IS_REQUESTER_EMAILS:
            vehicle, created = caches.vehicle_by_code(is_code, vehicle_type='isuzu')
            flag = None if not created else f"created new vehicle {is_code!r}"
            return vehicle, flag
        if plausible and is_vehicle and t_vehicle:
            vehicle, _created = caches.vehicle_by_code(t_code, vehicle_type='trailer')
            return vehicle, None
        vehicle, created = caches.vehicle_by_code(str(num))
        flag = f"vehicle code ambiguous/unlisted — used bare number {num!r}"
        return vehicle, flag

    return caches.get_unspecified_vehicle(), 'no vehicle reference found in source row'


def _new_report():
    return {'created': 0, 'flagged': [], 'skipped': [], 'created_ids': []}


def _import_row(env, caches, seen_uids, report, *, uid, employee_email, employee_name, description,
                 explicit_vehicle_code, vehicle_fallback_text, driver_name, technician_name,
                 state_raw, note, note_to_requester, rejection_reason,
                 request_date, completion_date, entry_dt, exit_dt,
                 odometer, tire_pressure, tire_bolts, work_statement, repairs_parts,
                 parts_cost, labor_cost, source_label):
    if uid and uid in seen_uids:
        report['skipped'].append((source_label, uid, 'duplicate UID (already imported from another source)'))
        return
    if uid and env['ksw.workshop.request'].search([('x_legacy_uid', '=', uid)], limit=1):
        report['skipped'].append((source_label, uid, 'already imported (present in DB)'))
        if uid:
            seen_uids.add(uid)
        return

    state = STATE_MAP.get((state_raw or '').strip())
    if not state:
        report['skipped'].append((source_label, uid, f'unresolvable status {state_raw!r}'))
        return

    flags = []
    employee, is_placeholder = caches.requester_employee(employee_email, employee_name)
    if is_placeholder:
        flags.append(f'no Odoo account for requester: linked to placeholder {employee.name!r}')

    vehicle, vflag = _resolve_vehicle(
        caches, explicit_vehicle_code, vehicle_fallback_text, requester_email=employee_email)
    if vflag:
        flags.append(vflag)

    driver = caches.employee_by_name(driver_name)
    if (driver_name or '').strip() and not driver:
        flags.append(f'driver name {driver_name!r} did not match exactly one employee')

    technician = caches.employee_by_name(technician_name)
    if (technician_name or '').strip() and not technician:
        flags.append(f'technician name {technician_name!r} did not match exactly one employee')

    vals = {
        'employee_id': employee.id,
        'vehicle_id': vehicle.id,
        'driver_id': driver.id if driver else False,
        'description': (description or '').strip() or 'Imported request',
        'state': state,
        'note': (note or '').strip() or False,
        'note_to_requester': (note_to_requester or '').strip() or False,
        'rejection_reason': (rejection_reason or '').strip() or False,
        'completion_date': completion_date,
        'entry_datetime': entry_dt,
        'exit_datetime': exit_dt,
        'odometer_reading': _int(odometer),
        'tire_pressure': (tire_pressure or '').strip() or False,
        'tire_bolts': (tire_bolts or '').strip() or False,
        'work_statement': (work_statement or '').strip() or False,
        'repairs_parts': (repairs_parts or '').strip() or False,
        'technician_id': technician.id if technician else False,
        'parts_cost': _float(parts_cost),
        'labor_cost': _float(labor_cost),
        'x_legacy_uid': uid or False,
        'x_imported': True,
        'x_legacy_requester_name': (employee_name or '').strip() or False,
        'x_legacy_requester_email': (employee_email or '').strip() or False,
    }

    try:
        with env.cr.savepoint():
            request = env['ksw.workshop.request'].with_context(
                mail_notrack=True, mail_create_nolog=True,
            ).sudo().create(vals)
            if request_date:
                env.cr.execute(
                    "UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s",
                    (request_date, request.id),
                )
    except Exception as exc:  # noqa: BLE001 - a bad row must not abort the whole import
        report['skipped'].append((source_label, uid, f'create failed: {exc}'))
        return

    if uid:
        seen_uids.add(uid)
    report['created'] += 1
    report['created_ids'].append(request.id)
    if flags:
        report['flagged'].append((source_label, uid, request.id, flags))


def _process_year_file(env, path, caches, seen_uids, report):
    source_label = path.rsplit('/', 1)[-1]
    with open(path, newline='', encoding='utf-8-sig') as f:
        first_line = f.readline()
        f.seek(0)
        has_header = first_line.strip().startswith('date,') or first_line.strip().startswith('"date"')
        reader = csv.DictReader(f) if has_header else csv.DictReader(f, fieldnames=YEAR_FILE_FIELDNAMES)
        for row in reader:
            if not _is_workshop_row(row.get('الجهة المعنية')):
                continue
            uid = (row.get('UID_HEADER') or '').strip()
            date_part = (row.get('date') or '').strip()
            time_part = (row.get('Time') or '').strip()
            request_date = _parse_date(f"{date_part} {time_part}".strip()) or _parse_date(date_part)
            _import_row(
                env, caches, seen_uids, report,
                uid=uid,
                employee_email=row.get('Email Address'),
                employee_name=row.get('اسم مقدم الطلب'),
                description=row.get('نوع الطلب'),
                explicit_vehicle_code=row.get('كود السيارة'),
                vehicle_fallback_text=row.get('المعني بالطلب'),
                driver_name=row.get('اسم السائق'),
                technician_name=row.get('الفني المسؤول لإصلاح المركبة'),
                state_raw=row.get('حالة الطلب'),
                note=row.get('ملاحظة'),
                note_to_requester=row.get('ملاحظة لمقدم الطلب'),
                rejection_reason=None,
                request_date=request_date,
                completion_date=_parse_date(row.get('تاريخ اكتمال الطلب')),
                entry_dt=_parse_date(f"{row.get('تاريخ الدخول', '')} {row.get('وقت الدخول', '')}".strip()),
                exit_dt=_parse_date(f"{row.get('تاريخ الخروج', '')} {row.get('وقت الخروج', '')}".strip()),
                odometer=row.get('رقم العداد'),
                tire_pressure=row.get('ضغط الكفرات'),
                tire_bolts=row.get('مسامير الكفرات'),
                work_statement=row.get('بيان المطلوب'),
                repairs_parts=row.get('الإصلاحات وقطع الغيار'),
                parts_cost=row.get('تكاليف قطع الغيار'),
                labor_cost=row.get('تكاليف أجور اليد(إن وجدت)'),
                source_label=source_label,
            )


def _process_master_file(env, path, caches, seen_uids, report):
    source_label = path.rsplit('/', 1)[-1]
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        # The status column name is duplicated in this file; csv.DictReader
        # keeps only the LAST occurrence under that key — which happens to
        # be the real one here (column 18, not the always-"جديد" column 13).
        for row in reader:
            if not _is_workshop_row(row.get("Request's Destination /الجهة المعنية")):
                continue
            uid = (row.get('UID_HEADER') or '').strip()
            if uid and uid in seen_uids:
                report['skipped'].append((source_label, uid, 'already covered by a year file'))
                continue
            _import_row(
                env, caches, seen_uids, report,
                uid=uid,
                employee_email=row.get('Email Address'),
                employee_name=row.get("Requester's Name / اسم مقدم الطلب"),
                description=row.get("Request's Description  /وصف الطلب "),
                explicit_vehicle_code=None,
                vehicle_fallback_text=row.get('For Whom /المعني بالطلب')
                    or row.get("Request's Description  /وصف الطلب "),
                driver_name=None,
                technician_name=None,
                state_raw=row.get('حالة الطلب'),
                note=row.get('ملاحظة الموظف'),
                note_to_requester=row.get('ملاحظة لمقدم الطلب'),
                rejection_reason=row.get('ملاحظة في حال رفض الطلب'),
                request_date=_parse_date(row.get('Timestamp')),
                completion_date=None,
                entry_dt=None,
                exit_dt=None,
                odometer=None,
                tire_pressure=None,
                tire_bolts=None,
                work_statement=None,
                repairs_parts=None,
                parts_cost=None,
                labor_cost=None,
                source_label=source_label,
            )


def import_workshop_history(env, year_files=(), master_file=None, commit=False):
    report = _new_report()
    caches = _Caches(env)
    seen_uids = set(env['ksw.workshop.request'].search([]).mapped('x_legacy_uid')) - {False}

    for path in year_files:
        _process_year_file(env, path, caches, seen_uids, report)

    if master_file:
        _process_master_file(env, master_file, caches, seen_uids, report)

    if commit:
        env.flush_all()
    else:
        env.cr.rollback()

    return report


def print_report(report, sample_size=15):
    print(f"Created: {report['created']}")

    flag_reasons = {}
    for _src, _uid, _rid, flags in report['flagged']:
        for flag in flags:
            key = flag.split(':')[0].split('(')[0][:40]
            flag_reasons[key] = flag_reasons.get(key, 0) + 1
    print(f"  ...of which flagged for review: {len(report['flagged'])}")
    for reason, count in sorted(flag_reasons.items(), key=lambda kv: -kv[1]):
        print(f"    - {reason}: {count}")
    print(f"  sample (first {sample_size}):")
    for src, uid, rid, flags in report['flagged'][:sample_size]:
        print(f"    [{src}] {uid} (id={rid}): {'; '.join(flags)}")

    skip_reasons = {}
    for _src, _uid, reason in report['skipped']:
        key = reason.split(':')[0].split('(')[0][:40]
        skip_reasons[key] = skip_reasons.get(key, 0) + 1
    print(f"\nSkipped: {len(report['skipped'])}")
    for reason, count in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
        print(f"  - {reason}: {count}")
