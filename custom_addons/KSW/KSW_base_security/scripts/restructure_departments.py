# -*- coding: utf-8 -*-
"""Restructure hr.department to what each unit really is (Nov 2026).

The department tree was carrying four questions at once: who reports to
whom, where people work, which legal entity employs them and which business
pays for them. It now answers only the first one. Location moves to
hr.work.location, money stays on posting groups / ledger entities (and later
analytic accounts), and the legal entity stays ONE company (re-confirmed
2026-11-13: Compound and Equipment Rental are on the same CR).

    Al-Kawther Group
    |-- Executive Office
    |-- Group Shared Services
    |   |-- Finance & Accounting / Accounting
    |   |-- Human Resources / Security, HR-IST, ALHAYAT-IST
    |   |-- IT & Development
    |   `-- Office Services
    |-- Kawther Sweet Water
    |   |-- Sales
    |   |-- Supply Chain / Heavy Vehicles / Supply Chain - Dammam, Supply Chain - Tabuk
    |   |                / Light Vehicles / Tariq's Branch, Disouky's Branch, VO
    |   |-- Maintenance, Workshop, Salwa
    |   `-- Al-Hayat Branch
    |-- Compound
    |-- Equipment Rental Department
    `-- Affiliated - Non-KSW / Jubail Schools, External Management

Every authority and money value that is inherited through parent_id (GM,
accounting approvers, posting group, ledger entity, home workshop) is
pinned on the department before it moves, wherever the new parent would
give a different answer. The report at the end diffs every active
employee's effective values before and after; anything other than the
department name changing is listed.

Departments and employees are matched by id AND name (ids are shared
between KSWCO and its dev copy); anything that does not match is skipped
and reported, never guessed.

    # dry run: prints the report, rolls back
    .venv/bin/python odoo-bin shell -c KSW_dev.conf --http-port=18070 \
        < custom_addons/KSW/KSW_base_security/scripts/restructure_departments.py

    # apply
    KSW_APPLY=1 .venv/bin/python odoo-bin shell -c KSW_dev.conf --http-port=18070 \
        < custom_addons/KSW/KSW_base_security/scripts/restructure_departments.py

Re-running is safe: new departments are found by name before being created,
and every write sets a target value.
"""
import os

APPLY = os.environ.get('KSW_APPLY') == '1'

# Existing departments: id -> (expected current en name, new en, new ar, parent key)
# parent key is a NEW department key below, an existing id, or None.
EXISTING = {
    3: ('Sales', 'Sales', 'المبيعات', 'ksw'),
    277: ('Finance', 'Finance & Accounting', 'المالية والمحاسبة', 'shared'),
    292: ('Accounting', 'Accounting', 'المحاسبة', 277),
    278: ('HR', 'Human Resources', 'الموارد البشرية', 'shared'),
    293: ('Housing', 'Security', 'الأمن والحراسة', 278),
    # IST = Saudi employees with special treatment, working remotely; HR manages them
    294: ('HR-IST', 'HR-IST', 'HR-IST', 278),
    291: ('ALHAYAT-IST', 'ALHAYAT-IST', 'ALHAYAT-IST', 278),
    279: ('Workshop', 'Workshop', 'الورشة', 'ksw'),
    280: ('Maintenance', 'Maintenance', 'الصيانة', 'ksw'),
    283: ('Supply Chain', 'Supply Chain - Dammam', 'سلاسل الإمداد - الدمام', 'heavy'),
    284: ('Tabuk', 'Supply Chain - Tabuk', 'سلاسل الإمداد - تبوك', 'heavy'),
    289: ('Salwa', 'Salwa', 'سلوى', 'ksw'),
    287: ('ALHAYAT', 'Al-Hayat Branch', 'فرع الحياة', 'ksw'),
    296: ("Tariq's Branch", "Tariq's Branch", 'فرع طارق', 'light'),
    297: ("Disouky's Branch", "Disouky's Branch", 'فرع الدسوقي', 'light'),
    298: ('VO', 'VO', 'VO', 'light'),
    285: ('Compound', 'Compound', 'كامب الجبيل', 'root'),
    290: ('Equipment Rental Department', 'Equipment Rental Department', 'تأجير المعدات', 'root'),
    288: ('Non-K', 'Affiliated - Non-KSW', 'جهات تابعة خارج الكوثر', 'root'),
}
# Names an earlier version of this script gave (applied on dev 2026-11-13)
PREVIOUS = {294: ('Saudi Staff (Remote) - Kawther',), 291: ('Saudi Staff (Remote) - Al-Hayat',)}
# New departments: key -> (en, ar, parent key, manager employee id)
NEW = [
    ('root', 'Al-Kawther Group', 'مجموعة الكوثر', None, 9753),          # Hussain, CEO
    ('exec', 'Executive Office', 'المكتب التنفيذي', 'root', 9753),
    ('shared', 'Group Shared Services', 'الخدمات المشتركة للمجموعة', 'root', None),
    ('it', 'IT & Development', 'تقنية المعلومات والتطوير', 'shared', 9938),  # Mohammed Jafar Sadiq
    ('office', 'Office Services', 'الخدمات المكتبية', 'shared', None),
    ('ksw', 'Kawther Sweet Water', 'الكوثر للمياه', 'root', 9943),     # Mohammed Amin, GM
    # headings only: Mishal and Azouni each run their own branch
    ('sc', 'Supply Chain', 'سلاسل الإمداد', 'ksw', None),
    ('heavy', 'Heavy Vehicles', 'المركبات الثقيلة', 'sc', None),
    ('light', 'Light Vehicles', 'المركبات الخفيفة', 'sc', None),
    ('schools', 'Jubail Schools', 'مدارس الجبيل', 288, 9859),
    ('external', 'External Management', 'إدارات خارجية', 288, 9859),
]
# Manager changes on existing departments: id -> employee id
MANAGERS = {
    3: 9942,     # Sales: Mortdoh, now the sales manager (was Mohammed Amin)
    287: 9777,   # Al-Hayat Branch: Jehad M. Shakhs, its head
    289: 9752,   # Salwa: Majdi Samaha (none set; its one employee reports to him)
    291: 9859,   # Saudi Staff Al-Hayat: Kadem; all five already report to him
    288: 9859,   # Affiliated: Kadem; all ten already report to him
}
# Emptied by the moves below, or empty already; archived, never deleted.
ARCHIVE = {1: 'Administration', 2: 'Management', 4: 'Research & Development',
           281: 'Nabia', 282: 'non-Main Office', 286: 'IST', 295: 'HR-Management',
           299: 'Testing', 2079: 'Unassigned (migrated)'}

# Employee moves: id -> (expected name fragment, department key or id)
MOVES = {
    9753: ('HUSSAIN ABD RAB', 'exec'),
    9943: ('MOHAMMED AMIN', 'ksw'),
    9938: ('JAFAR SADIQ', 'it'),
    9945: ('ALBASTI', 'it'),
    9937: ('al mahmoud', 293),
    9930: ('SHAMIM', 'office'),
    9942: ('MORTDOH', 3),
    11038: ('Ali H. Shakhs', 285),
    10773: ('JABEDUL ISLAM', 296),
    10774: ('HAMADA KHALAF', 296),
    11046: ('TAMER MOHAMED', 296),
    10776: ('EMAD ALY', 283),
    # Non-K split (user, 2026-11-13); the unsure ones stay external for now
    9905: ('ALIKOYA', 'schools'), 9908: ('MAHMOUD AHMED ADAM', 'schools'),
    9912: ('MOHAMED AHMED MOHAMED NADA', 'schools'), 9907: ('RIASAT', 'schools'),
    9906: ('JEHAD MOHAMMED', 'external'), 9924: ('صادق', 'external'),
    9903: ('BARKAT', 'external'), 9904: ('KHOKON', 'external'),
    9900: ('AZHAR', 'external'), 9902: ('SHAHID', 'external'),
}
HR_MANAGEMENT, HR = 295, 278          # HR-Management members fold into HR
TESTING = 299
KADEM = 9859
# Managed by HR (user, 2026-11-13): manager + time off approver -> Kadem
HR_MANAGED = {11038: 'Ali H. Shakhs'}
# Jehad M. Shakhs heads Al-Hayat, under Kawther Sweet Water: Mohammed Amin manages him
AMIN, AMIN_MANAGED = 9943, {9777: 'Jehad M. Shakhs'}
# Not people: archived with the Testing department
JUNK_EMPLOYEES = {10771: 'Payslip for june 2026', 11064: 'Nabia',
                  None: ('MGR Employee', 'SUB Employee', 'OTHER Employee')}
ADMIN_EMPLOYEE, ADMIN_EMPLOYEE_TO = 1, 'it'   # employee 'Admin' (user 1120, not base.user_admin)
HAYAT_WORKSHOP_CODE = 'HYT'

# Locations: name -> (ar, department ids whose members work there)
LOCATIONS = {
    'Nabiya': ('النابية', []),        # site of Kawther Sweet Water; who works there is not recorded
    'Jubail': ('الجبيل', [285, 'schools']),
    'Al-Hassa': ('الأحساء', [287]),
    'Dammam': ('الدمام', [283]),
    'Tabuk': ('تبوك', [284]),
    'Salwa': ('سلوى', [289]),
}

PINNED = ('x_gm_id', 'x_accountant_ids', 'x_posting_group_id', 'x_ledger_entity_id', 'x_workshop_id')
report = []


def say(msg):
    report.append(msg)
    print(msg)


def chain_value(dept, field):
    """What dept effectively gets for field by walking parent_id, like the
    code that reads it does (empty = falls to the company default)."""
    while dept:
        if dept[field]:
            return dept[field]
        dept = dept.parent_id
    return dept.browse()


def snapshot(env):
    Dept = env['hr.department']
    have = [f for f in PINNED if f in Dept._fields]
    Posting = env['ksw.payroll.posting'] if 'ksw.payroll.posting' in env else None
    Workshop = env['ksw.workshop'] if 'ksw.workshop' in env and 'x_workshop_id' in Dept._fields else None
    out = {}
    for emp in env['hr.employee'].search([]):
        d = emp.department_id
        row = {
            'department': d.complete_name or '',
            'manager': emp.parent_id.name or '',
            'time off approver': emp.leave_manager_id.login or '',
            'GM': d.x_effective_gm_id.name or '',
            'accountants': ', '.join(sorted(d.x_effective_accountant_ids.mapped('name'))),
        }
        if Posting is not None:
            company = emp.company_id
            staff = Posting._staff_entity(emp, company)
            row['ledger entity'] = staff.display_name or ''
            row['posting group'] = Posting._posting_group(emp, staff, Posting._default_entity(company)).display_name or ''
        if Workshop is not None:
            row['home workshop'] = Workshop._for_department(d).display_name if d else ''
        out[emp.id] = row
    return out, have


def find_dept(env, key, new_ids):
    return env['hr.department'].browse(new_ids[key] if isinstance(key, str) else key)


def run(env):
    env = env(context=dict(env.context, lang='en_US', tracking_disable=True), su=True)
    Dept, Emp = env['hr.department'], env['hr.employee']
    before, pinned_fields = snapshot(env)
    skipped = []

    # Al-Hayat's own workshop, set first so the pin below protects it
    if 'x_workshop_id' in Dept._fields and 'ksw.workshop' in env:
        hayat_ws = env['ksw.workshop'].search([('code', '=', HAYAT_WORKSHOP_CODE)], limit=1)
        hayat = Dept.browse(287).exists()
        if hayat_ws and hayat:
            hayat.x_workshop_id = hayat_ws
            # intended change: make it the baseline so the diff shows only surprises
            for emp in Emp.search([('department_id', 'child_of', hayat.id)]):
                before[emp.id]['home workshop'] = hayat_ws.display_name
            say('Al-Hayat Branch home workshop -> %s' % hayat_ws.display_name)
    else:
        say('Workshop sites not installed here: Al-Hayat home workshop not set.')

    # 1. check existing departments are what we think they are
    for did, (old, *_rest) in EXISTING.items():
        d = Dept.browse(did).exists()
        if not d or d.name not in (old, _rest[0], *PREVIOUS.get(did, ())):
            skipped.append('department %s: expected %r, found %r' % (did, old, d.name if d else None))

    # 2. effective values per existing department, before anything moves
    old_effective = {d.id: {f: chain_value(d, f) for f in pinned_fields}
                     for d in Dept.search([])}

    # 3. new departments (found by name first: re-runnable)
    new_ids = {}
    for key, en, ar, parent, manager in NEW:
        parent_rec = find_dept(env, parent, new_ids) if parent else Dept
        d = Dept.search([('name', '=', en), ('parent_id', '=', parent_rec.id or False),
                         ('id', 'not in', list(EXISTING))], limit=1)
        vals = {'name': en, 'parent_id': parent_rec.id or False,
                'manager_id': Emp.browse(manager).exists().id or False if manager else False}
        if d:
            d.write(vals)
        else:
            d = Dept.create(vals)
            say('created %s' % en)
        d.with_context(lang='ar_001').name = ar
        new_ids[key] = d.id
    # GM never moves (user, 2026-11-13: Mohammed Amin keeps approving shared services)
    amin = Emp.browse(9943)
    for key in ('root', 'exec', 'shared', 'it', 'office', 'ksw', 'sc', 'heavy', 'light'):
        Dept.browse(new_ids[key]).x_gm_id = amin

    # 4. re-parent / rename existing departments
    for did, (old, en, ar, parent) in EXISTING.items():
        d = Dept.browse(did).exists()
        if not d or d.name not in (old, en, *PREVIOUS.get(did, ())):
            continue
        d.write({'name': en, 'parent_id': find_dept(env, parent, new_ids).id if parent else False})
        d.with_context(lang='ar_001').name = ar
    for did, emp_id in MANAGERS.items():
        d, e = Dept.browse(did).exists(), Emp.browse(emp_id).exists()
        if d and e:
            d.manager_id = e

    # 5. pin anything the new parent would answer differently
    env.flush_all()
    for d in Dept.search([('id', 'in', list(old_effective))]):
        for f in pinned_fields:
            was, now = old_effective[d.id][f], chain_value(d, f)
            if was != now:
                d[f] = was
                say('pinned %s on %s: %s' % (f, d.name, ', '.join(was.mapped('display_name')) or '(company default)'))
    # the two Jubail Schools / External units sit under Affiliated, which carries Separate Salaries

    # 6. employee moves
    for eid, (frag, target) in MOVES.items():
        e = Emp.browse(eid).exists()
        if not e or frag.lower() not in e.name.lower():
            skipped.append('employee %s: expected %r, found %r' % (eid, frag, e.name if e else None))
            continue
        e.department_id = find_dept(env, target, new_ids)
    for e in Emp.search([('department_id', '=', HR_MANAGEMENT)]):
        e.department_id = HR
    kadem = Emp.browse(KADEM)
    for eid, name in HR_MANAGED.items():
        e = Emp.browse(eid).exists()
        if not e or e.name != name:
            skipped.append('employee %s: expected %r' % (eid, name))
            continue
        e.write({'parent_id': kadem.id, 'leave_manager_id': kadem.user_id.id})
    amin_rec = Emp.browse(AMIN)
    for eid, name in AMIN_MANAGED.items():
        e = Emp.browse(eid).exists()
        if not e or e.name != name:
            skipped.append('employee %s: expected %r' % (eid, name))
            continue
        e.write({'parent_id': amin_rec.id, 'leave_manager_id': amin_rec.user_id.id})

    # 7. test employees out of the live roster
    # the Admin account's employee belongs to IT, wherever it sits today
    admin = Emp.browse(ADMIN_EMPLOYEE).exists()
    if admin.name == 'Admin' and admin.department_id.id in ARCHIVE:
        admin.department_id = new_ids[ADMIN_EMPLOYEE_TO]
    junk = Emp.search([('department_id', '=', TESTING)])
    for eid, name in JUNK_EMPLOYEES.items():
        if eid is None:
            junk |= Emp.search([('name', 'in', list(name))])
        else:
            e = Emp.browse(eid).exists()
            if e and e.name == name:
                junk |= e
    for e in junk:
        if env['hr.payslip'].search_count([('employee_id', '=', e.id)]) or env['hr.attendance'].search_count([('employee_id', '=', e.id)]) \
                or env['hr.leave'].search_count([('employee_id', '=', e.id)]):
            skipped.append('test employee %s has payroll/attendance/leave history: left active' % e.name)
            junk -= e
    if junk:
        junk.write({'active': False})
        say('archived %d test employees: %s' % (len(junk), ', '.join(sorted(junk.mapped('name')))))

    # 8. work locations
    company = env.ref('base.main_company')
    for name, (ar, depts) in LOCATIONS.items():
        loc = env['hr.work.location'].search([('name', '=', name)], limit=1) or env['hr.work.location'].create(
            {'name': name, 'location_type': 'office', 'address_id': company.partner_id.id, 'company_id': company.id})
        ids = [find_dept(env, k, new_ids).id for k in depts]
        emps = Emp.search([('department_id', 'child_of', ids)]) if ids else Emp
        emps.write({'work_location_id': loc.id})
        say('work location %s: %d employees' % (name, len(emps)))

    # 9. archive the emptied ones
    # children before parents, so an emptied parent is not blocked by them
    for d in Dept.browse(list(ARCHIVE)).exists().sorted(lambda r: -len(r.parent_path or '')):
        name = ARCHIVE[d.id]
        if not d.active:
            continue
        left = Emp.search_count([('department_id', '=', d.id)])
        kids = Dept.search_count([('parent_id', '=', d.id)])
        if left or kids or d.name != name:
            skipped.append('not archived %s: %d employees, %d children' % (d.name, left, kids))
            continue
        d.active = False
        say('archived department %s' % name)

    # 10. report
    env.flush_all()
    env.invalidate_all()
    after, _ = snapshot(env)
    changes = {}
    for eid, row in after.items():
        old = before.get(eid)
        if not old:
            continue
        diff = {k: (old.get(k), v) for k, v in row.items() if k != 'department' and old.get(k) != v}
        if diff:
            changes[eid] = diff
    say('\n=== %d employees: only the department name changed for %d; %d other changes ==='
        % (len(after), len(after) - len(changes), len(changes)))
    for eid, diff in sorted(changes.items(), key=lambda x: after[x[0]]['department']):
        say('  %s [%s]' % (Emp.browse(eid).name, after[eid]['department']))
        for k, (a, b) in diff.items():
            say('      %-17s %s  ->  %s' % (k, a or '-', b or '-'))
    say('\n=== tree ===')
    for d in Dept.search([]).sorted('complete_name'):
        say('  %-62s %3d  mgr: %-28s GM: %s' % (
            d.complete_name, Emp.search_count([('department_id', '=', d.id)]),
            (d.manager_id.name or '')[:28], d.x_effective_gm_id.name or ''))
    if skipped:
        say('\n=== skipped ===')
        for s in skipped:
            say('  ' + s)


run(env)
if APPLY:
    env.cr.commit()
    print('\nCOMMITTED.')
else:
    env.cr.rollback()
    print('\nDry run: rolled back. KSW_APPLY=1 to write.')
