"""Repair request/completion dates on imported workshop requests.

Why this exists
---------------
`_parse_date` originally accepted only the year files' 12-hour format
(`9/12/2022` + `2:55:53 PM`) and a bare `%m/%d/%Y`. The master file's
`Timestamp` column is **24-hour** — `8/25/2022 16:38:23` — so it matched
neither, `_parse_date` returned None, and `_import_row`'s
`if request_date:` block never ran. Every row that came from the master
file therefore kept `create_date` = the moment of import.

That is all 3,598 requests from 2026: they were stamped 2026-08-23, so every
month/quarter/year report showed the entire year piled into one day. The
master file also carried no completion date at all, which left
`duration_days` at 0 for the whole year.

`_parse_date` now accepts the 24-hour format too, so a fresh import would not
repeat this. This script repairs the rows already in the database, from a
per-year export (which has proper `date` / `Time` / `تاريخ اكتمال الطلب`
columns), matched on `x_legacy_uid`.

Raw SQL for the two date columns: `create_date` is an ORM magic field, and
this is the same approach `import_history.py` uses to stamp it. `duration_days`
is a *stored* compute depending on `create_date`, so it is recomputed through
the ORM afterwards rather than derived in SQL — Python's `timedelta.days`
truncates toward negative infinity and `date_part('day', ...)` does not, and
the compute is the authority.

Usage (from `odoo-bin shell -c KSW_dev.conf --no-http`):

    >>> exec(open('custom_addons/KSW/KSW_workshop/scripts/backfill_request_dates.py').read())
    >>> report = backfill_request_dates(
    ...     env,
    ...     year_files=['/home/odoo/Odoo/2026معاملات الورشة - الصفحة الرئيسية.csv'],
    ...     commit=False,
    ... )
    >>> print_date_report(report)
"""
import csv
from datetime import datetime

DATE_FORMATS = (
    '%m/%d/%Y %I:%M:%S %p',   # year files: '9/12/2022 2:55:53 PM'
    '%m/%d/%Y %H:%M:%S',      # master file: '8/25/2022 16:38:23'
    '%m/%d/%Y',               # date only
)


def _parse(value):
    value = (value or '').strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _read_dates(path):
    """Yield (uid, request_date, completion_date) for rows carrying a date."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            uid = (row.get('UID_HEADER') or '').strip()
            if not uid:
                continue
            date_part = (row.get('date') or '').strip()
            time_part = (row.get('Time') or '').strip()
            # 2026 rows carry a date with an empty Time; day-level accuracy is
            # all a month/quarter/year report needs.
            request_date = _parse(f"{date_part} {time_part}".strip()) or _parse(date_part)
            completion_date = _parse(row.get('تاريخ اكتمال الطلب'))
            if request_date or completion_date:
                yield uid, request_date, completion_date


def backfill_request_dates(env, year_files=(), commit=False):
    report = {'csv_rows': 0, 'no_matching_request': 0,
              'create_date_fixed': 0, 'completion_date_set': 0,
              'unchanged': 0, 'moved_months': {}}

    by_uid = {}
    for path in year_files:
        for uid, request_date, completion_date in _read_dates(path):
            report['csv_rows'] += 1
            by_uid[uid] = (request_date, completion_date)

    Request = env['ksw.workshop.request'].sudo()
    touched_ids = []

    for uid, (request_date, completion_date) in by_uid.items():
        request = Request.search([('x_legacy_uid', '=', uid)], limit=1)
        if not request:
            report['no_matching_request'] += 1
            continue

        changed = False
        if request_date and request.create_date != request_date:
            env.cr.execute(
                "UPDATE ksw_workshop_request SET create_date = %s WHERE id = %s",
                (request_date, request.id))
            report['create_date_fixed'] += 1
            key = request_date.strftime('%Y-%m')
            report['moved_months'][key] = report['moved_months'].get(key, 0) + 1
            changed = True

        # Only fill a completion date, never overwrite one already recorded in
        # Odoo — a person may have completed the request here since the import.
        if completion_date and not request.completion_date:
            env.cr.execute(
                "UPDATE ksw_workshop_request SET completion_date = %s WHERE id = %s",
                (completion_date, request.id))
            report['completion_date_set'] += 1
            changed = True

        if changed:
            touched_ids.append(request.id)
        else:
            report['unchanged'] += 1

    # duration_days is a stored compute over create_date/completion_date, and
    # the SQL above went behind the ORM's back. Recompute it through the ORM so
    # the value matches what the compute would produce.
    if touched_ids:
        touched = Request.browse(touched_ids)
        touched.invalidate_recordset()
        touched.modified(['create_date', 'completion_date'])
        env.flush_all()
    report['duration_recomputed'] = len(touched_ids)

    if commit:
        env.cr.commit()
    else:
        env.cr.rollback()

    return report


def print_date_report(report):
    print(f"CSV rows with a date        : {report['csv_rows']}")
    print(f"create_date corrected       : {report['create_date_fixed']}")
    print(f"completion_date filled      : {report['completion_date_set']}")
    print(f"duration_days recomputed    : {report.get('duration_recomputed', 0)}")
    print(f"already correct (untouched) : {report['unchanged']}")
    print(f"UID not found in Odoo       : {report['no_matching_request']}")
    print("Resulting months:")
    for month, count in sorted(report['moved_months'].items()):
        print(f"  {month}: {count}")
