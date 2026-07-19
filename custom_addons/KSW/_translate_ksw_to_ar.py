#!/usr/bin/env python3
"""Translate all KSW module .po files to Arabic (ar_001).

Applies the three-tier precedence engine from _ar_engine.py to EVERY entry:
  1. Curated domain glossary (D dict)   — exact match → approved term
  2. Official Odoo core ar.po           — exact match → core verbatim (tashkeel stripped)
  3. Auto-cleanup of current Arabic     — tashkeel strip + concept propagation

ALL entries are processed (not just empty ones), so:
- Short labels get the approved terminology from the master glossary
- Longer strings (help texts, tooltips, error messages) get tashkeel removed and
  superseded concept tokens replaced in-place, preserving HTML, {{ vars }}, %()s
- No string is left with tashkeel or superseded terminology

Run from anywhere:
    python3 /home/odoo/Odoo/odoo/custom_addons/KSW/_translate_ksw_to_ar.py
"""
import os
import sys
import polib

BASE = '/home/odoo/Odoo/odoo/custom_addons/KSW'
ODOO_ROOT = '/home/odoo/Odoo/odoo'

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ar_engine as E  # noqa: E402

CORE_AR_PATHS = [
    f'{ODOO_ROOT}/addons/base/i18n/ar.po',
    f'{ODOO_ROOT}/addons/web/i18n/ar.po',
    f'{ODOO_ROOT}/addons/mail/i18n/ar.po',
    f'{ODOO_ROOT}/addons/hr/i18n/ar.po',
    f'{ODOO_ROOT}/addons/hr_holidays/i18n/ar.po',
    f'{ODOO_ROOT}/addons/hr_attendance/i18n/ar.po',
    f'{ODOO_ROOT}/addons/account/i18n/ar.po',
    f'{ODOO_ROOT}/addons/resource/i18n/ar.po',
    f'{ODOO_ROOT}/addons/calendar/i18n/ar.po',
    (f'{ODOO_ROOT}/custom_addons/'
     'Odoo Mates - hr_payroll_community-19.0.1.0.1/om_hr_payroll/i18n/ar.po'),
]

KSW_MODULES = [
    'KSW_annual_leave',
    'KSW_commissions',
    'KSW_deduction',
    'KSW_eos_leave',
    'KSW_leave_approval',
    'KSW_unpaid_leave',
    'KSW_payroll',
    'KSW_attendance_leave',
    'KSW_attendance_sheet',
    'KSW_attendance_report',
    'KSW_base_security',
    'KSW_ext_sync',
    'KSW_leave_types',
    'KSW_working_schedule',
]


def load_core_map():
    """Build {en: ar} from Odoo core ar.po files. First hit wins (base loaded first)."""
    core_map = {}
    loaded = 0
    for path in CORE_AR_PATHS:
        if not os.path.exists(path):
            continue
        try:
            po = polib.pofile(path)
            for entry in po:
                if (not entry.obsolete and entry.msgid and entry.msgstr
                        and entry.msgid not in core_map):
                    core_map[entry.msgid] = entry.msgstr
            loaded += 1
        except Exception as exc:
            mod = os.path.basename(os.path.dirname(os.path.dirname(path)))
            print(f'  skip core {mod}: {exc}')
    print(f'  loaded {len(core_map):,} terms from {loaded} core modules')
    return core_map


def translate_po(input_path, output_path, core_map):
    """Apply the three-tier engine to every entry; save as ar_001.po."""
    po = polib.pofile(input_path)
    po.metadata.update({
        'Language': 'ar_001',
        'Plural-Forms': (
            'nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : '
            'n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);'
        ),
        'X-Generator': 'KSW ar-engine v2',
    })

    counts = {'changed': 0, 'matched-core': 0, 'changed (auto)': 0, 'unchanged': 0}

    for entry in po:
        if entry.obsolete or not entry.msgid:
            continue

        if entry.msgid_plural:
            cur_sg = entry.msgstr_plural.get(0, '')
            cur_pl = entry.msgstr_plural.get(1, '')
            sg, status, _, _ = E.propose(entry.msgid, [cur_sg] if cur_sg else [], core_map)
            pl, _, _, _ = E.propose(entry.msgid_plural, [cur_pl] if cur_pl else [], core_map)
            entry.msgstr_plural = {0: sg, 1: pl or sg}
            counts[status] = counts.get(status, 0) + 1
        else:
            cur = [entry.msgstr] if entry.msgstr else []
            proposed, status, _, _ = E.propose(entry.msgid, cur, core_map)
            entry.msgstr = proposed
            counts[status] = counts.get(status, 0) + 1

    po.save(output_path)
    return counts, sum(counts.values())


# om_hr_payroll terms that KSW_payroll must override (Egyptian-dialect → Saudi Arabic).
# Appended to KSW_payroll/i18n/ar_001.po after every run so they survive re-runs.
# Format: (annotations_block, msgid, msgstr)
OM_HR_PAYROLL_OVERRIDES = [
    (
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_hr_payroll_root',
        'Payroll', 'الرواتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_view_hr_payslip_form\n'
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_department_tree',
        'Employee Payslips', 'كشوف الراتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_hr_payslip_by_employees',
        'Generate Payslips', 'إنشاء كشوف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_input__payslip_id\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_line__slip_id\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_worked_days__payslip_id',
        'Pay Slip', 'كشف الراتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_payslip_lines_contribution_register',
        'PaySlip Lines', 'بنود كشف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__payslip_run_id',
        'Payslip Batches', 'كشوف الرواتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_employee__payslip_count',
        'Payslip Count', 'عدد كشوف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__input_line_ids',
        'Payslip Inputs', 'مدخلات كشف الراتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.act_contribution_reg_payslip_lines\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__line_ids',
        'Payslip Lines', 'بنود كشف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__name',
        'Payslip Name', 'اسم كشف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__worked_days_line_ids',
        'Payslip Worked Days', 'أيام العمل في كشف الراتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.act_hr_employee_payslip_list\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_employee__slip_ids\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_run__slip_ids',
        'Payslips', 'كشوف الراتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_hr_payslip_run_tree\n'
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_hr_payslip_run',
        'Payslips Batches', 'كشوف الرواتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_hr_salary_rule_category\n'
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_hr_salary_rule_category',
        'Salary Rule Categories', 'فئات قواعد الرواتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_rule_input__input_id',
        'Salary Rule Input', 'مدخلات قاعدة الرواتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_salary_rule_form\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payroll_structure__rule_ids\n'
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_action_hr_salary_rule_form',
        'Salary Rules', 'قواعد الرواتب',
    ),
    (
        '#: model:ir.actions.act_window,name:om_hr_payroll.action_view_hr_payroll_structure_list_form\n'
        '#: model:ir.ui.menu,name:om_hr_payroll.menu_hr_payroll_structure_view',
        'Salary Structures', 'هياكل الرواتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_line__appears_on_payslip\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_salary_rule__appears_on_payslip',
        'Appears on Payslip', 'يظهر في كشف الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_line__child_ids\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_salary_rule__child_ids',
        'Child Salary Rule', 'قواعد الراتب الفرعية',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip__details_by_salary_rule_category',
        'Details by Salary Rule Category', 'تفاصيل حسب فئات قواعد الراتب',
    ),
    (
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_payslip_line__parent_rule_id\n'
        '#: model:ir.model.fields,field_description:om_hr_payroll.field_hr_salary_rule__parent_rule_id',
        'Parent Salary Rule', 'قاعدة الراتب الأصل',
    ),
]

OVERRIDE_HEADER = (
    '\n# '
    + '-' * 75 + '\n'
    '# om_hr_payroll terminology overrides — KSW_payroll loads after om_hr_payroll\n'
    '# so these entries win on every language load / Settings > Update.\n'
    '# ' + '-' * 75 + '\n'
)


def append_om_hr_payroll_overrides(output_path):
    """Append om_hr_payroll override entries to output_path (KSW_payroll ar_001.po)."""
    lines = [OVERRIDE_HEADER]
    for annotations, msgid, msgstr in OM_HR_PAYROLL_OVERRIDES:
        lines.append(
            f'\n#. module: KSW_payroll\n'
            f'{annotations}\n'
            f'msgid "{msgid}"\n'
            f'msgstr "{msgstr}"\n'
        )
    with open(output_path, 'a', encoding='utf-8') as fh:
        fh.write(''.join(lines))
    print(f'  + appended {len(OM_HR_PAYROLL_OVERRIDES)} om_hr_payroll overrides')


def main():
    print('Loading Odoo core Arabic map...')
    core_map = load_core_map()
    print()

    grand = {}
    for mod in KSW_MODULES:
        inp = f'{BASE}/{mod}/i18n/ar.po'
        out = f'{BASE}/{mod}/i18n/ar_001.po'
        if not os.path.exists(inp):
            print(f'skip (no source ar.po): {mod}')
            continue
        counts, total = translate_po(inp, out, core_map)
        parts = ', '.join(f'{k}={v}' for k, v in sorted(counts.items()) if v)
        print(f'{mod}: {total} entries — {parts}')
        for k, v in counts.items():
            grand[k] = grand.get(k, 0) + v
        if mod == 'KSW_payroll':
            append_om_hr_payroll_overrides(out)

    print()
    total_all = sum(grand.values())
    parts = ', '.join(f'{k}={v}' for k, v in sorted(grand.items()))
    print(f'TOTAL: {total_all} entries — {parts}')


if __name__ == '__main__':
    main()
