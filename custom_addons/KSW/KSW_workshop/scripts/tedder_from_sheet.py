# -*- coding: utf-8 -*-
"""Find imported workshop requests that are about a tedder (the tank) but were
put on a head (T###/IS###), and move them to «تيدر N».

Why: the history import and the 2026-10-14 sheet catch-up resolved a row on the
first number in «المعني بالطلب», with no tedder rule at all, so «انيل..خزان ١٧١»
landed on T171 and «تريلا 171 رشيد» + «خزان 169 يحتاج لحام» landed on T171
instead of تيدر 169. «المعني بالطلب» is never stored in Odoo, so the sheet
files are re-read and joined on x_legacy_uid.

Which tank a row is about (number after خزان/خزن/تيدر, «رقم» optional):
- MOVE   : «نوع الطلب» names exactly one tank and no head (تريلا/سيارة/راس N);
           or it names no vehicle and «المعني بالطلب» names exactly one tank
           and no head.
- REVIEW : the description names a tank AND a head («تريلا 138 وخزان 187»),
           two different tanks, or head work beside the tank («بدل زيت مكينه
           وه خزان 107 عايز لحام»). Listed, never moved.

Run in an Odoo shell; dry run unless commit=True. Idempotent (a request
already on its tedder is counted, not touched); each move posts a chatter note:
    exec(open('<this file>').read()); report = repair(env, commit=False)
"""
import csv
import re

import os as _os

# TEDDER_SOURCE_DIR: where the sheet files sit (the prod container cannot see
# the host's /home/odoo/Odoo, so they are staged into it first).
_SRC = _os.environ.get('TEDDER_SOURCE_DIR', '/home/odoo/Odoo')
SOURCES_CSV = [_os.path.join(_SRC, name) for name in (
    'معاملات الورشة 2022 - الصفحة الرئيسية.csv',
    'معاملات الورشة 2023 - الصفحة الرئيسية.csv',
    'معاملات الورشة 2024 - الصفحة الرئيسية.csv',
    'معاملات الورشة 2025 - Sheet1.csv',
    '2026معاملات الورشة - الصفحة الرئيسية.csv',
)]
SOURCE_XLSX = _os.path.join(_SRC, 'معاملات الورشة - 14-10-2026.xlsx')

# The rule (_tank_of_request) and YEAR_FILE_FIELDNAMES live in the importer,
# so a repaired row and a newly synced row are judged the same way.
import odoo.addons.KSW_workshop as _ksw_workshop
exec(open(_os.environ.get('IMPORT_HISTORY_PATH') or _os.path.join(
    _ksw_workshop.__path__[0], 'scripts', 'import_history.py')).read())


def read_rows():
    """uid -> (who, desc). The newest source wins for a UID present twice."""
    rows = {}
    for path in SOURCES_CSV:
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                first = f.readline()
                f.seek(0)
                if first.startswith('date,') or first.startswith('"date"'):
                    reader = csv.DictReader(f)
                else:
                    reader = csv.DictReader(f, fieldnames=YEAR_FILE_FIELDNAMES)
                for r in reader:
                    uid = (r.get('UID_HEADER') or '').strip()
                    if uid:
                        rows[uid] = (r.get('المعني بالطلب') or '', r.get('نوع الطلب') or '')
        except FileNotFoundError:
            pass
    try:
        import openpyxl
        wb = openpyxl.load_workbook(SOURCE_XLSX, read_only=True, data_only=True)
        it = wb['الصفحة الرئيسية'].iter_rows(values_only=True)
        header = list(next(it))
        i_uid, i_who, i_desc = (header.index(k) for k in ('UID_HEADER', 'المعني بالطلب', 'نوع الطلب'))
        for r in it:
            uid = str(r[i_uid] or '').strip()
            if uid:
                rows[uid] = (str(r[i_who] or ''), str(r[i_desc] or ''))
    except FileNotFoundError:
        pass
    return rows



def repair(env, commit=False, only_uids=None):
    from markupsafe import Markup
    Vehicle, Request = env['ksw.fleet.vehicle'].sudo(), env['ksw.workshop.request'].sudo()
    tedders = {v.name: v for v in Vehicle.search([('vehicle_type', '=', 'tank')])}
    report = {'moved': [], 'already': 0, 'review': [], 'no_tedder': []}
    for uid, (who, desc) in sorted(read_rows().items()):
        if only_uids and uid not in only_uids:
            continue
        verdict, value = _tank_of_request(who, desc)
        if not verdict:
            continue
        req = Request.search([('x_legacy_uid', '=', uid)], limit=1)
        if not req:
            continue
        if verdict == 'review':
            if req.vehicle_id.vehicle_type != 'tank':
                report['review'].append((req.name, uid, req.vehicle_id.name, value, who.strip(), desc.strip()[:80]))
            continue
        tedder = tedders.get('تيدر %d' % value)
        if req.vehicle_id == tedder:
            report['already'] += 1
            continue
        if not tedder:
            report['no_tedder'].append((req.name, uid, req.vehicle_id.name, value))
            continue
        old = req.vehicle_id
        report['moved'].append((req.name, uid, old.name, tedder.name))
        if commit:
            # Raw write: the request may be completed/locked, and the vehicle
            # change must not re-run scope checks or driver snapshots.
            env.cr.execute('UPDATE ksw_workshop_request SET vehicle_id = %s WHERE id = %s',
                           (tedder.id, req.id))
            req.invalidate_recordset(['vehicle_id'])
            req.with_context(mail_notrack=True).message_post(
                body=Markup('Vehicle corrected <b>%(old)s</b> → <b>%(new)s</b>: the original request '
                            'is about the tank (المعني بالطلب: «%(who)s», نوع الطلب: «%(desc)s»).')
                % {'old': old.name, 'new': tedder.name, 'who': who.strip(), 'desc': desc.strip()[:120]},
                subtype_xmlid='mail.mt_note')
    if commit:
        env.cr.commit()
    return report
