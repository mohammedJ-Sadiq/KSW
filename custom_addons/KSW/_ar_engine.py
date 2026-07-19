#!/usr/bin/env python3
"""Shared Arabic-translation engine for the KSW modules.

One source of truth for the terminology decisions, used by both
`_build_ar_glossary.py` (produces the reviewable master CSV) and
`_translate_ksw_to_ar.py` (writes the ar_001.po files).

Precedence for the proposed Arabic of any English source string:
  1. exact hit in the curated glossary D               → status 'changed'
  2. exact hit in official Odoo core ar.po (not shielded) → status 'matched-core'
  3. otherwise: clean up the current Arabic — strip tashkeel + propagate the
     decided concept terms into compound strings          → 'changed (auto)'
     (or 'unchanged' if nothing moved)

The concept-propagation in step 3 is GATED ON THE ENGLISH msgid, never applied
blindly to Arabic, so e.g. «موافقة»→«اعتماد» only fires on Approve* strings.
"""
import re

# ── 1. Curated domain glossary: en -> (proposed_ar, source, note) ────────────
# Sources: labour-law (نظام العمل/التأمينات/حماية الأجور), ksa-hr (العرف الوظيفي
# السعودي), sap-oracle (عرف أنظمة ERP), odoo-core, unify (توحيد صيغ).
SEC_PAYROLL = [
    ('Payslip', 'كشف الراتب', 'sap-oracle', 'قرار المستخدم: كشف الراتب (وليس قسيمة)'),
    ('Pay Slip', 'كشف الراتب', 'sap-oracle', 'متغير آخر لـ Payslip'),
    ('Payslip Batch', 'كشف الرواتب', 'sap-oracle', 'قرار المستخدم: الدفعة الشهرية = كشف الرواتب'),
    ('Payslip Batches', 'كشوف الرواتب', 'sap-oracle', ''),
    ('Payslip Batch — Bank Account NET Total', 'كشف الرواتب — إجمالي الصافي حسب البنك', 'sap-oracle', ''),
    ('Payslip Batch — Skipped Employee Log', 'كشف الرواتب — سجل الموظفين المستبعدين', 'ksa-hr', '«مستبعد» بدل «متخطى»'),
    ('Skipped', 'مستبعد', 'ksa-hr', ''),
    ('Payslip Line', 'بند كشف الراتب', 'sap-oracle', ''),
    ('Payslip Lines', 'بنود كشف الراتب', 'sap-oracle', ''),
    ('Search Payslip Lines', 'البحث في بنود كشف الراتب', 'sap-oracle', ''),
    ('Payslip Worked Days', 'أيام العمل في كشف الراتب', 'sap-oracle', ''),
    ('Payslip Name', 'اسم كشف الراتب', 'sap-oracle', ''),
    ('Payslip: {{ object.number }}', 'كشف الراتب: {{ object.number }}', 'sap-oracle', ''),
    ('Payslip Auto-Email Template', 'قالب الإرسال التلقائي لكشف الراتب', 'sap-oracle', ''),
    ('Auto-Email Payslip', 'إرسال كشف الراتب تلقائياً', 'sap-oracle', ''),
    ('Vacation Payslip', 'كشف راتب الإجازة', 'sap-oracle', ''),
    ('Vacation Payslips', 'كشوف رواتب الإجازة', 'sap-oracle', ''),
    ('EOS Payslip', 'كشف راتب نهاية الخدمة', 'unify', 'يوحّد «قسيمة/راتب نهاية الخدمة»'),
    ('Payslip File Order', 'ترتيب ملف كشف الراتب', 'sap-oracle', ''),
    ('Forwarded From Payslip', 'مرحّل من كشف راتب سابق', 'sap-oracle', 'المصطلح المحاسبي للترحيل'),
    ('Salary Rule', 'قاعدة الراتب', 'ksa-hr', 'om الأساسي: «قاعدة مرتبات» — سجل مصري'),
    ('Wage', 'الأجر', 'labour-law', 'مصطلح نظام العمل'),
    ('Basic Wage', 'الراتب الأساسي', 'ksa-hr', ''),
    ('Hourly Wage', 'الأجر بالساعة', 'ksa-hr', ''),
    ('Daily Rate', 'الأجر اليومي', 'unify', 'يوحّد «الأجر اليومي» و«المعدل اليومي»'),
    ('Worked Days', 'أيام العمل', 'ksa-hr', 'الأساسي مطوَّل'),
    ('Attendance Deduction', 'خصم الغياب والتأخير', 'ksa-hr', 'وصف وظيفي دقيق؛ الحالي «خصم الحضور» عكس المعنى'),
    ('Attendance Deduction Breakdown', 'تفصيل خصم الغياب والتأخير', 'ksa-hr', ''),
    ('Overtime', 'العمل الإضافي', 'labour-law', 'المادة 107'),
    ('Saturday Overtime Credit', 'رصيد العمل الإضافي للسبت', 'labour-law', ''),
    ('Saturday Short-Shift Overtime', 'العمل الإضافي لوردية السبت القصيرة', 'labour-law', ''),
    ('Social Insurance (GOSI)', 'التأمينات الاجتماعية', 'labour-law', ''),
    ('GOSI Rate (%)', 'نسبة التأمينات الاجتماعية (٪)', 'labour-law', ''),
    ('WPS (Bank Transfer)', 'حماية الأجور (تحويل بنكي)', 'labour-law', 'نظام حماية الأجور'),
    ('WPS Text File Generator', 'مولّد الملف النصي لنظام حماية الأجور (WPS)', 'labour-law', ''),
    ('Allowance', 'بدل', 'ksa-hr', 'الأساسي «علاوة» = زيادة راتب — خطأ سياقي'),
    ('Transportation Allowance', 'بدل النقل', 'ksa-hr', 'صيغة العقود السعودية'),
    ('Net Salary', 'صافي الراتب', 'ksa-hr', ''),
]

SEC_EOS = [
    ('End-of-Service (Saudi Art. 84–88)', 'مكافأة نهاية الخدمة (المواد 84–88 من نظام العمل)', 'unify', ''),
    ('EOS Notice Pay (Deduction)', 'تعويض مهلة الإشعار (خصم)', 'labour-law', 'المادة 76'),
    ('Notice Pay (Deduction)', 'تعويض مهلة الإشعار (خصم)', 'labour-law', ''),
    ('Last Wage (SAR)', 'الأجر الأخير (ريال)', 'labour-law', 'المادة 84'),
]

SEC_LEAVES = [
    ('Unpaid Leave', 'الإجازة بدون أجر', 'labour-law', 'المادة 116'),
    ('Unpaid Leave Days', 'أيام الإجازة بدون أجر', 'labour-law', ''),
    ('Is Unpaid Leave', 'إجازة بدون أجر', 'labour-law', ''),
    ('Unpaid Portion (Days)', 'الجزء بدون أجر (أيام)', 'labour-law', ''),
    ('Sick Leave', 'الإجازة المرضية', 'labour-law', 'المادة 117'),
    ('Time Off', 'الإجازات', 'odoo-core', ''),
    ('Leave', 'إجازة', 'ksa-hr', 'الأساسي «مغادرة» — غير مناسبة'),
    ('Return Date', 'تاريخ المباشرة', 'ksa-hr', '«مباشرة العمل» = العودة من الإجازة'),
    ('Return Status', 'حالة المباشرة', 'ksa-hr', ''),
    ('Return Confirmed', 'تمت المباشرة', 'ksa-hr', ''),
    ('✅ Confirm Return', '✅ تأكيد المباشرة', 'ksa-hr', ''),
    ('✅ Return Confirmed', '✅ تمت المباشرة', 'ksa-hr', ''),
    ('Full Balance Clearance', 'تصفية كامل الرصيد', 'unify', ''),
    ('Financial Consideration for Excess Leave', 'المقابل المالي عن الإجازة الزائدة', 'unify', ''),
    ('Visa Cost Recovery for Excess Leave', 'استرداد تكلفة التأشيرة عن الإجازة الزائدة', 'unify', ''),
    ('Annual Leave – Multi-Step Approval', 'الإجازة السنوية — اعتماد متعدد المراحل', 'ksa-hr', 'سجل «اعتماد» + إزالة التشكيل'),
    ('Unpaid Leave – Multi-Step Approval', 'الإجازة بدون أجر — اعتماد متعدد المراحل', 'ksa-hr', ''),
]

SEC_APPROVAL = [
    ('Approve', 'اعتماد', 'ksa-hr', 'الأساسي «موافقة»'),
    ('Approved', 'معتمد', 'ksa-hr', ''),
    ('Refused', 'مرفوض', 'sap-oracle', ''),
    ('Confirmed', 'مؤكد', 'sap-oracle', ''),
    ('Cancelled', 'ملغي', 'sap-oracle', ''),
    ('Submit Request', 'تقديم الطلب', 'ksa-hr', ''),
    ('Pending DM Approval', 'بانتظار اعتماد المدير المباشر', 'unify', 'توحيد «بانتظار»'),
    ('Pending HR Approval', 'بانتظار اعتماد الموارد البشرية', 'unify', ''),
    ('Pending GM Initial', 'بانتظار الاعتماد الأولي للمدير العام', 'unify', ''),
    ('Pending GM Final', 'بانتظار الاعتماد النهائي للمدير العام', 'unify', ''),
    ('Pending Accounting', 'بانتظار اعتماد المحاسبة', 'unify', ''),
    ('Pending HR Confirmation', 'بانتظار تأكيد الموارد البشرية', 'ksa-hr', 'تصحيح: الخطوة 6 صارت للموارد البشرية'),
    ('HR Approver', 'معتمد الموارد البشرية', 'unify', ''),
    ('My Pending Approvals', 'طلبات بانتظار اعتمادي', 'ksa-hr', ''),
    ('Pending My Action', 'بانتظار إجرائي', 'ksa-hr', ''),
    ('Confirmed By', 'تم التأكيد بواسطة', 'unify', ''),
    # Button labels: fix gender agreement broken by موافقة→اعتماد propagation.
    ('✅ Initial Approve (GM)', '✅ الاعتماد الأولي (المدير العام)', 'ksa-hr', 'اتفاق التذكير مع «اعتماد»'),
    ('GM (Initial Review)', 'المدير العام (مراجعة أولية)', 'ksa-hr', ''),
]

SEC_DEDUCTION = [
    ('Salary Advance', 'سلفة راتب', 'ksa-hr', ''),
    ('Advances & Other', 'السلف وغيرها', 'ksa-hr', ''),
    ('Internal Penalty', 'جزاء داخلي', 'labour-law', 'لائحة الجزاءات'),
    ('Gov. Penalty', 'غرامة حكومية', 'labour-law', ''),
    ('Penalties', 'الجزاءات والغرامات', 'labour-law', ''),
    ('Paid', 'مسدد', 'ksa-hr', 'سياق الأقساط = سداد'),
    ('Paid / Total', 'المسدد / الإجمالي', 'ksa-hr', ''),
    ('Total Paid', 'إجمالي المسدد', 'ksa-hr', ''),
    ('Mark Paid', 'تسجيل السداد', 'ksa-hr', ''),
    ('Record Loan Payment', 'تسجيل سداد قرض', 'ksa-hr', ''),
    ('Loan Disbursement Officer', 'مسؤول صرف القروض', 'ksa-hr', ''),
    ('Full (incl. active installments)', 'تعديل كامل (شامل الأقساط النشطة)', 'ksa-hr', 'تصحيح: الحالي «تعديل» ناقص'),
]

SEC_ATTENDANCE = [
    ('Attendance', 'الحضور والانصراف', 'ksa-hr', 'المصطلح الوظيفي الكامل'),
    ('Attendance Sheet', 'كشف الحضور والانصراف', 'ksa-hr', ''),
    ('Attendance Sheets', 'كشوف الحضور والانصراف', 'ksa-hr', ''),
    ('Monthly Attendance Sheet', 'كشف الحضور والانصراف الشهري', 'ksa-hr', ''),
    ('Monthly Attendance Report', 'تقرير الحضور والانصراف الشهري', 'ksa-hr', ''),
    ('Attendance Record', 'سجل الحضور والانصراف', 'ksa-hr', ''),
    ('Early Leave', 'الانصراف المبكر', 'ksa-hr', ''),
    ('Early Leave Minutes', 'دقائق الانصراف المبكر', 'ksa-hr', ''),
    ('Early Leave (min)', 'انصراف مبكر (دقيقة)', 'ksa-hr', ''),
    ('Total Early Leave Minutes', 'إجمالي دقائق الانصراف المبكر', 'ksa-hr', ''),
    ('Skip Late/Early Leave Checks', 'تجاهل فحوصات التأخير/الانصراف المبكر', 'ksa-hr', ''),
    ('Biometric Device', 'جهاز البصمة', 'ksa-hr', 'الاستخدام السعودي: البصمة'),
    ('Biometric Device Details', 'بيانات جهاز البصمة', 'ksa-hr', ''),
    ('Biometric Attendance Sync Service', 'خدمة مزامنة حضور البصمة', 'ksa-hr', ''),
    ('Biometric Schedule Helper', 'مساعد جداول البصمة', 'ksa-hr', ''),
    ('Biometric: Stale Device Alert (every 30 minutes)', 'البصمة: تنبيه توقف الجهاز (كل 30 دقيقة)', 'ksa-hr', ''),
    ('Covered', 'مشمول بإجازة', 'ksa-hr', 'الحالي «مغطى» حرفية'),
    ('Covered by Time Off', 'مشمول بإجازة', 'ksa-hr', ''),
    ('No Record', 'لا يوجد سجل', 'ksa-hr', ''),
    ('Main Work Schedule', 'جدول الدوام الأساسي', 'ksa-hr', '«الدوام» = اللفظ السعودي'),
    ('Temporary Work Schedule', 'جدول دوام مؤقت', 'ksa-hr', ''),
]

SEC_COMMISSIONS = [
    ('Salesman', 'مندوب المبيعات', 'ksa-hr', '«البائع» = بائع محل'),
    ('Salesman & Collector', 'مندوب مبيعات ومحصل', 'ksa-hr', ''),
    ('Salesperson', 'مندوب المبيعات', 'unify', ''),
    ('Salesperson Profiles', 'ملفات مندوبي المبيعات', 'ksa-hr', ''),
    ('Amount', 'المبلغ', 'unify', 'يوحّد «القيمة» و«المبلغ»'),
]

SECTIONS = [
    ('الرواتب — Payroll', SEC_PAYROLL),
    ('نهاية الخدمة — End of Service', SEC_EOS),
    ('الإجازات — Leaves', SEC_LEAVES),
    ('سير الاعتماد — Approval workflow', SEC_APPROVAL),
    ('الخصومات — Deductions', SEC_DEDUCTION),
    ('الحضور والانصراف — Attendance', SEC_ATTENDANCE),
    ('العمولات وعامة — Commissions & misc', SEC_COMMISSIONS),
]

D = {}
D_SECTION = {}
for _title, _sec in SECTIONS:
    for _en, _ar, _src, _note in _sec:
        D[_en] = (_ar, _src, _note)
        D_SECTION[_en] = _title

# ── Shield list: core ar.po differs but is wrong/contradicts a decision ──────
KEEP_CURRENT = {
    '<strong>Bank Account</strong>': 'ترجمة Odoo الأساسية معطوبة (وسوم HTML معكوسة)',
    '<strong>Designation</strong>': 'الأساسي «التعيين» — المقصود المسمى الوظيفي',
    '<strong>Date From</strong>': 'قالب تقرير KSW؛ «من تاريخ» أوضح',
    '<strong>Date To</strong>': 'قالب تقرير KSW؛ «إلى تاريخ» أوضح',
    '<strong>Reference</strong>': 'الأساسي «رقم الإشارة» — غير مناسب',
    'Deduction': 'قرار المستخدم رقم 3: «خصم» وليس «الاستقطاعات»',
    'HR Approval': 'سجل «اعتماد» (الأساسي: موافقة)',
    'Time Off Approval': 'سجل «اعتماد» (الأساسي: الموافقة على طلب الإجازة)',
    'Officer': 'الأساسي «مستخدم» — خطأ؛ المقصود مسؤول',
    'State': 'الأساسي «الولاية» — المقصود حالة سير العمل',
    'Rate': 'سياق العمولات: «النسبة» لا «المعدل»',
    'Base': 'سياق العمولات: «الأساس» لا «قاعدة»',
    'Schedule': 'المقصود جدول الأقساط (اسم) لا «جدولة»',
    'Label': 'الأساسي «بطاقة عنوان» — «التسمية» صحيحة',
    'Selection': 'الأساسي «قائمة خيارات» — المقصود الاختيار',
    'Is System': 'الأساسي «نظام» — المقصود «نظامي»',
    'Break': 'الأساسي «فاصل» — المصطلح الوظيفي «استراحة»',
    'Worked Day': 'الأساسي مطوَّل',
    'Worked Hours': 'الأساسي «ساعات العمل المقضية» — مطوَّل',
    'Salary Computation': 'سجل «راتب» لا «مرتب»',
    'Total Payable': 'ترجمة الأساسي بها خطأ إملائي',
    'Receipts': 'المصطلح المحاسبي «المقبوضات»',
    'Day of Week': 'الأساسي «اليوم من الأسبوع» — ركيكة',
}

# ── tashkeel / concept cleanup ───────────────────────────────────────────────
# Harakat + superscript alef; these combine only with Arabic letters, so a
# global strip never touches Latin text, {{ vars }}, or %()s placeholders.
_TASHKEEL = re.compile('[ً-ْٰـ]')  # + tatweel U+0640

def strip_tashkeel(s):
    return _TASHKEEL.sub('', s)

# Always-safe normalisations, applied to every auto row regardless of English.
_GLOBAL_SUBS = [('في انتظار', 'بانتظار')]

def normalize(s):
    out = strip_tashkeel(s)
    for a, b in _GLOBAL_SUBS:
        out = out.replace(a, b)
    return out

# English-gated concept propagation. Each entry: (english-trigger predicate,
# list of (ar_from, ar_to) ordered longest-first). Only fires when the English
# msgid matches the trigger, so a token is never rewritten out of context.
def _has(*subs):
    return lambda enl: any(s in enl for s in subs)

_CONCEPT_RULES = [
    (_has('payslip'), [
        ('قسيمة الراتب', 'كشف الراتب'), ('قسائم الراتب', 'كشوف الراتب'),
        ('قسيمة راتب', 'كشف راتب'), ('قسائم رواتب', 'كشوف رواتب'),
        ('قسيمة', 'كشف'), ('قسائم', 'كشوف'),
    ]),
    (_has('unpaid'), [
        ('غير مدفوعة الأجر', 'بدون أجر'), ('غير المدفوعة', 'بدون أجر'),
        ('غير مدفوع الأجر', 'بدون أجر'), ('غير المدفوع', 'بدون أجر'),
        ('غير مدفوعة', 'بدون أجر'), ('غير مدفوع', 'بدون أجر'),
    ]),
    (_has('approv'), [
        ('الموافقة', 'الاعتماد'), ('موافقة', 'اعتماد'),
    ]),
    (_has('biometric'), [('الجهاز البيومتري', 'جهاز البصمة'),
                         ('البيومترية', 'البصمة'), ('البيومتري', 'البصمة')]),
    (_has('salesman', 'salesperson'), [('البائع', 'مندوب المبيعات'), ('المندوب', 'مندوب المبيعات')]),
    (_has('transport'), [('المواصلات', 'النقل')]),
    (_has('early'), [('الخروج المبكر', 'الانصراف المبكر'), ('خروج مبكر', 'انصراف مبكر')]),
    (_has('cover'), [('مغطاة', 'مشمولة بإجازة'), ('مغطى', 'مشمول بإجازة')]),
    # Attendance → الحضور والانصراف only in safe fixed compounds.
    (_has('attendance'), [
        ('كشوف الحضور', 'كشوف الحضور والانصراف'), ('كشف الحضور', 'كشف الحضور والانصراف'),
        ('سجل الحضور', 'سجل الحضور والانصراف'), ('تقرير الحضور', 'تقرير الحضور والانصراف'),
    ]),
]

def propagate_concepts(en, ar):
    enl = en.lower()
    out = ar
    for trigger, subs in _CONCEPT_RULES:
        if trigger(enl):
            for a, b in subs:
                out = out.replace(a, b)
    # standalone exact «الحضور» → full compound (guard: don't touch والانصراف form)
    if 'attendance' in enl and out.strip() == 'الحضور':
        out = 'الحضور والانصراف'
    return out


def propose(en, current_ar_variants, core_map):
    """Return (proposed_ar, status, source, note) for one English msgid.

    current_ar_variants: list of existing msgstr values (may be empty).
    core_map: {en: ar} from official Odoo core.
    """
    # 1. curated glossary (no-tashkeel policy applies to our own terms too)
    if en in D:
        ar, src, note = D[en]
        return strip_tashkeel(ar), 'changed', src, note
    # 2. official core (unless shielded); strip tashkeel per KSW policy
    if en in core_map and en not in KEEP_CURRENT and core_map[en].strip():
        return strip_tashkeel(core_map[en].strip()), 'matched-core', 'odoo-core', \
            'مطابقة الترجمة الرسمية لـ Odoo (قرار المستخدم رقم 4)'
    # 3. cleanup + propagation of the current translation
    cur = current_ar_variants[0] if current_ar_variants else ''
    proposed = propagate_concepts(en, normalize(cur))
    if en in KEEP_CURRENT:
        return proposed, ('changed (auto)' if proposed != cur else 'unchanged'), \
            'keep-current', KEEP_CURRENT[en]
    status = 'changed (auto)' if proposed != cur else 'unchanged'
    return proposed, status, 'auto', ''


# concept tokens that must NOT survive into any final proposed_ar (audit use)
SUPERSEDED_TOKENS = ['قسيمة', 'البيومتري', 'غير المدفوع', 'في انتظار', 'المواصلات', 'خروج مبكر']
