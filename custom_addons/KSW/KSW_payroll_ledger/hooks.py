"""The payroll posting configuration, built from BAS's practice.

Read off BAS's salary vouchers (Sep 2026: 26009158 / 26009187 / 26009232)
and the accountant's review of the October entries. Built once — on install,
or by migration 19.0.1.6.0 from the fields it replaces — and after that it is
the accountant's, in Accounting ▸ Configuration ▸ Payroll Posting.

Each wage account in use becomes a posting group: one row for the wage and
everything else, plus a row per allowance only where the allowance's own
account belongs to the same branch (3201… main office). That is exactly what
the code did before the tables existed, so the entries do not move.
"""
import logging

_logger = logging.getLogger(__name__)

# Allowance rules (BAS: each to its own account at the main office).
RULE_ACCOUNTS = {
    'HRA': '3201010002',             # بدل سكن العاملين
    'Travel': '3201010003',          # بدل انتقال العاملين
    'Mobile': '3201010004',          # بدل اتصال (جوال)
    'Other': '3201010011',           # بدلات اخرى
}
# One account for everyone: commissions paid inside a payslip clear the
# accrual the commission month booked (2107010001), not a second expense.
# A collection rep's fixed allowance is a commission expense at every branch.
FIXED_RULE_ACCOUNTS = {'KSW_COMMISSIONS': '2107010001', 'COLLECTION_COMM': '3203020006'}
CATEGORY_NAMES = {
    'HRA': ('Housing', 'بدل السكن'),
    'Travel': ('Transport', 'بدل الانتقال'),
    'Mobile': ('Mobile', 'بدل الجوال'),
    'Other': ('Other Allowances', 'بدلات أخرى'),
}
DEPARTMENT_ACCOUNTS = {
    'Compound': '3204010001',
    "Tariq's Branch": '3201010007',
    "Disouky's Branch": '3201010007',
    'HR-IST': '3201010010',
    'ALHAYAT-IST': '3205010005',
    'Non-K': '3201010010',
    'Equipment Rental Department': '3206010001',
    # the same departments after the Nov 2026 restructure
    # (KSW_base_security/scripts/restructure_departments.py)
    'Affiliated - Non-KSW': '3201010010',
}
HAYAT_DEPARTMENTS = ('ALHAYAT', 'Al-Hayat Branch')
HAYAT_BANK_PREFIX = 'Hayat'
HAYAT_EMPLOYEE_ACCOUNTS = {'50070': '3205010005', '50047': '3205010026'}
PERSONAL_ACCOUNT_RULES = ('GOSI', 'VACATION_GOSI')

ACCOUNTS = {
    'main': '3201010001', 'separate': '3201010010', 'payable': '2107010004',
    'hayat_wage': '3205010019', 'hayat_payable': '2107010008',
    'hayat_current': '1208010010', 'kawther_current': '2101030003',
}
GROUP_NAMES = {
    '3201010001': ('Main Office', 'الإدارة الرئيسية'),
    '3201010010': ('Separate Salaries (M)', 'رواتب مستقلة (M)'),
    '3201010007': ('Isuzu Drivers', 'سائقو الإيسوزو'),
    '3204010001': ('Jubail Camp', 'كامب الجبيل'),
    '3206010001': ('Transport', 'النقليات'),
    '3205010005': ('Al-Hayat Admin', 'إداريو الحياة'),
    '3205010019': ('Al-Hayat Factory', 'عمال مصنع الحياة'),
    '3205010026': ('Al-Hayat Ice', 'عمال ثلج الحياة'),
}


def post_init_hook(env):
    Account = env['account.account'].sudo()
    for company in env['res.company'].search([]):
        if not Account.search_count([('company_ids', 'in', company.id), ('code', '=', '3201010001')]):
            continue      # not on BAS's chart: nothing to seed
        env['ksw.payroll.ledger']._journal('SAL', company)
        env['ksw.payroll.ledger']._journal('COMM', company)
        build_posting_config(env, company, defaults_as_legacy(env, company))


def defaults_as_legacy(env, company):
    """The built-in mapping, in the shape the migration reads the old fields."""
    env = env(context=dict(env.context, active_test=False, lang='en_US'))
    Account = env['account.account'].sudo()

    def acc(code):
        return Account.search([('company_ids', 'in', company.id), ('code', '=', code)], limit=1).id
    Rule, Dept, Emp = env['hr.salary.rule'].sudo(), env['hr.department'].sudo(), env['hr.employee'].sudo()
    legacy = {'rule_accounts': {}, 'fixed_rules': {}, 'dept_accounts': {}, 'dept_hayat': set(),
              'bank_hayat': set(), 'emp_accounts': {}, 'emp_separate': set(), 'emp_allowance': {},
              'rule_personal': set(Rule.search([('code', 'in', PERSONAL_ACCOUNT_RULES)]).ids), 'params': {}}
    for code, account in RULE_ACCOUNTS.items():
        for rule in Rule.search([('code', '=', code)]):
            if acc(account):
                legacy['rule_accounts'][rule.id] = acc(account)
    for code, account in FIXED_RULE_ACCOUNTS.items():
        for rule in Rule.search([('code', '=', code)]):
            if acc(account):
                legacy['fixed_rules'][rule.id] = acc(account)
    for name, account in DEPARTMENT_ACCOUNTS.items():
        for dept in Dept.search([('name', '=', name)]):
            if acc(account):
                legacy['dept_accounts'][dept.id] = acc(account)
    legacy['dept_hayat'] = set(Dept.search([('name', 'in', HAYAT_DEPARTMENTS)]).ids)
    # Filtered here, not searched: res.partner.bank searches acc_number on its
    # sanitised form, which for Hayat WPS is the IBAN, not the label.
    legacy['bank_hayat'] = set(env['res.partner.bank'].sudo().search(
        [('partner_id', '=', company.partner_id.id)]).filtered(
        lambda b: (b.acc_number or '').lower().startswith(HAYAT_BANK_PREFIX.lower())).ids)
    for number, account in HAYAT_EMPLOYEE_ACCOUNTS.items():
        emp = Emp.search([('x_employee_no', '=', number)])
        if len(emp) == 1 and acc(account):
            legacy['emp_accounts'][emp.id] = acc(account)
    return legacy


def build_posting_config(env, company, legacy):
    """Entities, categories, posting groups and their map, from ``legacy``.

    Skipped when the company already has payroll entities: it is then the
    accountant's, and this never writes over it."""
    env = env(context=dict(env.context, active_test=False, lang='en_US'))
    Entity = env['ksw.payroll.entity'].sudo()
    if Entity.search_count([('company_id', '=', company.id)]):
        _logger.info('KSW_payroll_ledger: posting configuration already there for %s', company.name)
        return
    Account = env['account.account'].sudo()
    Group = env['ksw.payroll.posting.group'].sudo()
    Category = env['ksw.payroll.posting.category'].sudo()
    Rule = env['hr.salary.rule'].sudo()
    arabic = bool(env['res.lang']._lang_get('ar_001'))
    params = legacy.get('params') or {}

    def acc(key):
        code = params.get(key) or ACCOUNTS[key]
        return Account.search([('company_ids', 'in', company.id), ('code', '=', code)], limit=1)

    def translate(record, ar):
        if arabic and ar:
            record.update_field_translations('name', {'ar_001': ar})

    # Categories: one per allowance rule; the rule's own account becomes a row.
    allowances = []          # (category, account)
    for rule_id, account_id in legacy['rule_accounts'].items():
        rule = Rule.browse(rule_id).exists()
        account = Account.browse(account_id).exists()
        if not rule or not account:
            continue
        category = Category.search([('code', '=', rule.code)], limit=1)
        if not category:
            en, ar = CATEGORY_NAMES.get(rule.code, (rule.name, None))
            category = Category.create({'name': en, 'code': rule.code, 'sequence': len(allowances) + 1})
            translate(category, ar)
        rule.write({'x_posting_category_id': category.id, 'x_ledger_account_id': False})
        if (category, account) not in allowances:
            allowances.append((category, account))
    for rule_id, account_id in (legacy.get('fixed_rules') or {}).items():
        Rule.browse(rule_id).exists().write({'x_ledger_account_id': account_id})

    groups = {}

    def group_for(account, split):
        """The posting group for a wage account. ``split``: an allowance keeps its
        own account where that account is of the same branch (4-digit group)."""
        key = (account.id, split)
        if key not in groups:
            en, ar = GROUP_NAMES.get(account.code, (account.name, None))
            if split is False and account.code not in GROUP_NAMES and any(
                    (a.code or '')[:4] == (account.code or '')[:4] for _c, a in allowances):
                en = '%s (one account)' % en
            rows = [(0, 0, {'account_id': account.id})]
            if split:
                rows += [(0, 0, {'category_id': c.id, 'account_id': a.id}) for c, a in allowances
                         if (a.code or '')[:4] == (account.code or '')[:4]]
            group = Group.create({'name': en, 'map_ids': rows, 'sequence': len(groups) + 1})
            translate(group, ar)
            groups[key] = group
        return groups[key]

    main, separate = acc('main'), acc('separate')
    kawther = Entity.create({
        'name': 'Al-Kawther', 'company_id': company.id, 'is_default': True, 'sequence': 1,
        'payable_account_id': acc('payable').id, 'default_group_id': group_for(main, True).id})
    translate(kawther, 'الكوثر')
    m_group = group_for(separate, False)
    hayat = Entity
    if acc('hayat_payable') and acc('hayat_wage'):
        hayat = Entity.create({
            'name': 'Al-Hayat', 'company_id': company.id, 'sequence': 2,
            'payable_account_id': acc('hayat_payable').id,
            'default_group_id': group_for(acc('hayat_wage'), False).id})
        translate(hayat, 'الحياة')
        IC = env['ksw.payroll.intercompany'].sudo()
        IC.create({'payer_id': kawther.id, 'staff_entity_id': hayat.id, 'posting_group_id': m_group.id,
                   'note': "Al-Hayat's staff paid from Al-Kawther's accounts: «رواتب السعوديين M»."})
        IC.create({'payer_id': hayat.id, 'staff_entity_id': kawther.id,
                   'payer_account_id': acc('kawther_current').id, 'staff_account_id': acc('hayat_current').id,
                   'note': 'Al-Kawther staff paid by Al-Hayat: Al-Kawther owes Al-Hayat the net.'})
        env['hr.department'].sudo().browse(list(legacy['dept_hayat'])).exists().write(
            {'x_ledger_entity_id': hayat.id})
        env['res.partner.bank'].sudo().browse(list(legacy['bank_hayat'])).exists().write(
            {'x_ledger_entity_id': hayat.id})

    for dept_id, account_id in legacy['dept_accounts'].items():
        dept = env['hr.department'].sudo().browse(dept_id).exists()
        account = Account.browse(account_id).exists()
        if dept and account:
            entity = _dept_entity(dept, kawther)
            dept.x_posting_group_id = group_for(account, entity == kawther and account != separate)
    Emp = env['hr.employee'].sudo()
    for emp_id, account_id in legacy['emp_accounts'].items():
        emp = Emp.browse(emp_id).exists()
        account = Account.browse(account_id).exists()
        if emp and account:
            entity = _dept_entity(emp.department_id, kawther)
            emp.x_posting_group_id = group_for(account, entity == kawther and account != separate)
    for emp in Emp.browse(list(legacy['emp_separate'])).exists():
        if emp.id not in legacy['emp_accounts'] and _dept_entity(emp.department_id, kawther) == kawther:
            emp.x_posting_group_id = m_group
    for emp_id, account_id in legacy['emp_allowance'].items():
        emp = Emp.browse(emp_id).exists()
        if emp and Account.browse(account_id).exists():
            emp.x_posting_exception_ids = [(0, 0, {
                'category_id': c.id, 'account_id': account_id,
                'note': 'Allowances apart from his wage (migrated)'}) for c, _a in allowances]
    Rule.browse(list(legacy['rule_personal'])).exists().write({'x_ledger_deduction': 'own_account'})
    _logger.info('KSW_payroll_ledger: posting configuration built for %s: %s groups, %s categories',
                 company.name, len(groups), len(allowances))


def _dept_entity(dept, default):
    while dept:
        if dept.x_ledger_entity_id:
            return dept.x_ledger_entity_id
        dept = dept.parent_id
    return default
