"""One-off backfill of the technician name onto imported workshop requests.

Why this exists
---------------
`import_history.py` resolved the sheet's technician column through
`employee_by_name()`, which requires an exact match on exactly one
`hr.employee`. The sheet holds bare first names (سلطان, بلال, جلال) while
`hr.employee` holds full names, so **nothing ever matched** — `technician_id`
is NULL on all 17,079 imported rows — and the name itself was not stored
anywhere, so "who did this work?" was unanswerable.

This reads the names back out of the original CSVs and writes them to
`x_legacy_technician_name`, which feeds the stored `technician_label` used by
the "By Technician" report.

Why a script and not a migration
--------------------------------
The source CSVs live on the host, not inside the production container, so a
post-migrate step could not read them. Same shape as `import_history.py`:
run by hand, `commit=False` first.

Coverage
--------
Only the per-year files carry a technician column. The master file (which is
where the 2026 rows come from) does not — `import_history.py` passes
`technician_name=None` for it — so 2026 requests stay empty permanently.
Expect roughly 4,330 rows across 2022-2024 plus whatever 2025 carries.

Usage (from `odoo-bin shell -c KSW_dev.conf --no-http`):

    >>> exec(open('custom_addons/KSW/KSW_workshop/scripts/backfill_technician.py').read())
    >>> report = backfill_technicians(
    ...     env,
    ...     year_files=[
    ...         '/home/odoo/Odoo/معاملات الورشة 2022 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2023 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2024 - الصفحة الرئيسية.csv',
    ...         '/home/odoo/Odoo/معاملات الورشة 2025 - Sheet1.csv',
    ...     ],
    ...     commit=False,
    ... )
    >>> print_technician_report(report)
"""
import csv

TECHNICIAN_COLUMN = 'الفني المسؤول لإصلاح المركبة'

# Same field list and header sniffing as import_history.py — the 2025 export
# has no header row, so the column order doubles as the fieldnames.
YEAR_FILE_FIELDNAMES = [
    'UID_HEADER', 'date', 'Time', 'Email Address', 'اسم مقدم الطلب',
    'الجهة المعنية', 'المعني بالطلب', 'نوع الطلب', 'ملاحظة', 'حالة الطلب',
    'ملاحظة لمقدم الطلب', 'الحالة المنقولة لمدير النظام', 'تاريخ اكتمال الطلب',
    'مدة الإنجاز ', 'التقارير تبدأ من هنا', 'تاريخ الدخول', 'وقت الدخول',
    'رقم اللوحة', 'كود السيارة', 'اسم السائق', 'جهة العمل', 'تاريخ الخروج',
    'وقت الخروج', 'رقم العداد', 'ضغط الكفرات', 'مسامير الكفرات', 'بيان المطلوب',
    'الإصلاحات وقطع الغيار', 'الفني المسؤول لإصلاح المركبة',
    'تكاليف قطع الغيار', 'تكاليف أجور اليد(إن وجدت)',
    'تاريخ إدخال البيانات', 'تاريخ أخر تحديث للبيانات',
]


def _read_technicians(path):
    """Yield (uid, technician_name) for every row that carries a technician."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        first_line = f.readline()
        f.seek(0)
        has_header = (first_line.strip().startswith('date,')
                      or first_line.strip().startswith('"date"'))
        reader = csv.DictReader(f) if has_header else csv.DictReader(
            f, fieldnames=YEAR_FILE_FIELDNAMES)
        for row in reader:
            uid = (row.get('UID_HEADER') or '').strip()
            name = (row.get(TECHNICIAN_COLUMN) or '').strip()
            if uid and name:
                yield uid, name


def backfill_technicians(env, year_files=(), commit=False):
    report = {'rows_in_csv': 0, 'updated': 0, 'no_matching_request': 0,
              'already_set': 0, 'names': {}}

    by_uid = {}
    for path in year_files:
        for uid, name in _read_technicians(path):
            report['rows_in_csv'] += 1
            by_uid[uid] = name
            report['names'][name] = report['names'].get(name, 0) + 1

    Request = env['ksw.workshop.request'].sudo()
    for uid, name in by_uid.items():
        request = Request.search([('x_legacy_uid', '=', uid)], limit=1)
        if not request:
            report['no_matching_request'] += 1
            continue
        if request.x_legacy_technician_name:
            report['already_set'] += 1
            continue
        # Raw SQL: 4k+ rows, and going through write() would recompute and
        # re-log per record. technician_label is a stored compute depending on
        # this column, so it is refreshed explicitly below rather than left
        # stale — the ORM never sees this UPDATE.
        env.cr.execute(
            "UPDATE ksw_workshop_request SET x_legacy_technician_name = %s WHERE id = %s",
            (name, request.id),
        )
        report['updated'] += 1

    # Refresh the stored compute for every row this touched. Doing it in one
    # statement rather than via modified()/recompute keeps it off the ORM,
    # consistent with the UPDATE above.
    env.cr.execute("""
        UPDATE ksw_workshop_request
        SET technician_label = x_legacy_technician_name
        WHERE technician_id IS NULL
          AND x_legacy_technician_name IS NOT NULL
          AND technician_label IS DISTINCT FROM x_legacy_technician_name
    """)
    report['labels_refreshed'] = env.cr.rowcount

    if commit:
        env.cr.commit()
    else:
        env.cr.rollback()

    return report


def print_technician_report(report):
    print(f"Rows with a technician in the CSVs : {report['rows_in_csv']}")
    print(f"Distinct technician names          : {len(report['names'])}")
    print(f"Requests updated                   : {report['updated']}")
    print(f"  already had a name (skipped)     : {report['already_set']}")
    print(f"  UID not found in Odoo            : {report['no_matching_request']}")
    print(f"technician_label rows refreshed    : {report.get('labels_refreshed', 0)}")
    print("Top technicians:")
    for name, count in sorted(report['names'].items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {name}: {count}")
