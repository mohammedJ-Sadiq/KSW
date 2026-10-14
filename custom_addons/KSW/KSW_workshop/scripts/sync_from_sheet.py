"""Catch Odoo up with the workshop Google Sheet, from an .xlsx export.

The Google Form kept being used after the Odoo go-live, so the sheet runs
ahead of the database. This brings it level, by hand, from an export of the
sheet's main tab ("الصفحة الرئيسية", the 41-column year-file layout):

- **New UIDs** are created through `import_history._import_row`, unchanged —
  same requester / vehicle / placeholder rules as the original import.
- **Existing imported requests** (matched on `x_legacy_uid`) take the sheet's
  status, but only *forward* (new → in progress → completed / rejected). A
  sheet status behind Odoo's is reported, never applied: someone moved it in
  Odoo. Completion date and the two notes are filled only when empty.
- Native Odoo requests (`x_imported` false) are never touched.
- A UID that appears more than once in the sheet with different statuses
  (the sheet holds a few completed + rejected copies of one submission) is
  reported and left alone, both for creation and update.

Silent on purpose: `create()` posts a "New workshop request" message to every
workshop manager, and `mail_notrack` does not stop an explicit `message_post`
(Odoo 19 Pitfalls #93: the 2026-08-23 import queued 17,079 emails). The
notification is switched off for this process only, and restored after.
State is written directly rather than through `action_complete`, so no
requester notification and no stock issue (KSW_workshop_stock hooks the
action, not the write).

Usage (from `odoo-bin shell`):

    >>> exec(open('custom_addons/KSW/KSW_workshop/scripts/sync_from_sheet.py').read())
    >>> report = sync_from_sheet(env, '/path/معاملات الورشة - 14-10-2026.xlsx', commit=False)
    >>> print_sync_report(report)
    >>> report = sync_from_sheet(env, '/path/...xlsx', commit=True)
    >>> env.cr.commit()
"""
import collections
import os
from datetime import datetime

import openpyxl

import odoo.addons.KSW_workshop as _ksw_workshop

# Set IMPORT_HISTORY_PATH before exec() to run against a copy staged next to
# this file, rather than whatever snapshot the server's addons path holds.
exec(open(globals().get('IMPORT_HISTORY_PATH') or os.path.join(
    _ksw_workshop.__path__[0], 'scripts', 'import_history.py')).read())

SHEET_NAME = 'الصفحة الرئيسية'
STATE_ORDER = {'new': 0, 'in_progress': 1, 'completed': 2, 'rejected': 2}


def _cell_str(value):
    """The importer parses CSV strings; render xlsx cells the same way."""
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%m/%d/%Y %H:%M:%S')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_sheet(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = wb[SHEET_NAME].iter_rows(values_only=True)
    header = list(next(rows))
    out = []
    for raw in rows:
        row = {h: _cell_str(v) for h, v in zip(header, raw) if h}
        row['_completion_dt'] = raw[header.index('تاريخ اكتمال الطلب')]
        if (row.get('UID_HEADER') or '').strip():
            out.append(row)
    return out


def sync_from_sheet(env, path, commit=False):
    report = _new_report()
    report.update({'updated': [], 'behind': [], 'conflicting_uids': []})
    Request = env['ksw.workshop.request']
    rows = [r for r in _read_sheet(path) if _is_workshop_row(r.get('الجهة المعنية'))]

    statuses = collections.defaultdict(set)
    for r in rows:
        statuses[r['UID_HEADER'].strip()].add(STATE_MAP.get(r['حالة الطلب'].strip()))
    conflicting = {uid for uid, s in statuses.items() if len(s) > 1}
    report['conflicting_uids'] = sorted(conflicting)

    existing = {
        rec.x_legacy_uid: rec
        for rec in Request.sudo().with_context(active_test=False).search([('x_legacy_uid', '!=', False)])
    }

    RequestClass = type(Request)
    original_notify = RequestClass._notify_workshop_managers
    RequestClass._notify_workshop_managers = lambda self: None
    try:
        caches = _Caches(env)
        seen_uids = set(existing)
        for r in rows:
            uid = r['UID_HEADER'].strip()
            if uid in conflicting:
                continue
            rec = existing.get(uid)
            if rec is None:
                _create_from_row(env, caches, seen_uids, report, r, path)
            elif rec.x_imported:
                _update_from_row(report, rec, r)
        if commit:
            env.flush_all()
        else:
            env.cr.rollback()
    finally:
        RequestClass._notify_workshop_managers = original_notify
    return report


def _create_from_row(env, caches, seen_uids, report, row, path):
    date_part = row.get('date', '').strip()
    _import_row(
        env, caches, seen_uids, report,
        uid=row['UID_HEADER'].strip(),
        employee_email=row.get('Email Address'),
        employee_name=row.get('اسم مقدم الطلب'),
        description=row.get('نوع الطلب'),
        explicit_vehicle_code=row.get('كود السيارة'),
        vehicle_fallback_text=row.get('المعني بالطلب') or row.get('نوع الطلب'),
        driver_name=row.get('اسم السائق'),
        technician_name=row.get('الفني المسؤول لإصلاح المركبة'),
        state_raw=row.get('حالة الطلب'),
        note=row.get('ملاحظة'),
        note_to_requester=row.get('ملاحظة لمقدم الطلب'),
        rejection_reason=None,
        request_date=_parse_date(f"{date_part} {row.get('Time', '')}".strip()) or _parse_date(date_part),
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
        source_label=os.path.basename(path),
    )


def _update_from_row(report, rec, row):
    vals = {}
    sheet_state = STATE_MAP.get(row['حالة الطلب'].strip())
    if sheet_state and sheet_state != rec.state:
        if STATE_ORDER[sheet_state] > STATE_ORDER[rec.state]:
            vals['state'] = sheet_state
        else:
            report['behind'].append((rec.x_legacy_uid, rec.name, rec.state, sheet_state))
    final_state = vals.get('state', rec.state)
    completion = row['_completion_dt']
    if final_state == 'completed' and not rec.completion_date and isinstance(completion, datetime):
        vals['completion_date'] = _sheet_time_to_utc(completion.replace(microsecond=0))
    for field, column in (('note', 'ملاحظة'), ('note_to_requester', 'ملاحظة لمقدم الطلب')):
        text = (row.get(column) or '').strip()
        if text and not rec[field]:
            vals[field] = text
    if vals:
        rec.with_context(mail_notrack=True, tracking_disable=True).sudo().write(vals)
        report['updated'].append((rec.x_legacy_uid, rec.name, sorted(vals)))


def print_sync_report(report, sample_size=15):
    print_report(report, sample_size=sample_size)
    print(f"\nUpdated existing: {len(report['updated'])}")
    for uid, name, fields_ in report['updated'][:sample_size]:
        print(f"  {uid} {name}: {', '.join(fields_)}")
    print(f"\nSheet behind Odoo (not applied): {len(report['behind'])}")
    for uid, name, db_state, sheet_state in report['behind']:
        print(f"  {uid} {name}: Odoo {db_state}, sheet {sheet_state}")
    print(f"\nUIDs with conflicting statuses in the sheet (skipped): {report['conflicting_uids']}")
