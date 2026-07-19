#!/usr/bin/env python3
"""Fill remaining empty msgstr entries in KSW ar_001.po files.

This is a second-pass script that handles strings not covered by the
glossary in _translate_ksw_to_ar.py — primarily complex HTML snippets,
long help texts, and module-specific terminology.

Run from anywhere:
    python3 /home/odoo/Odoo/odoo/custom_addons/KSW/_fill_remaining_ar.py
"""
import os
import polib

BASE = '/home/odoo/Odoo/odoo/custom_addons/KSW'

# ---------------------------------------------------------------------------
# Remaining translations not covered by the main glossary
# ---------------------------------------------------------------------------
REMAINING = {}

# ---- KSW_payroll ----------------------------------------------------------
REMAINING.update({
    # Python format string for report name (keep Python syntax intact)
    "('Annual Vacation Report - %s' % (object.employee_id.name))":
        "('تقرير الإجازة السنوية - %s' % (object.employee_id.name))",

    # Email template body
    '<div style="margin: 0px; padding: 0px;">\n                    <p>Dear <t t-out="object.employee_id.name or \'\'"/>,</p>\n                    <p>Please find the attached payslip for the period: <t t-out="object.date_from"/> - <t t-out="object.date_to"/></p>\n                    <p>In case of any queries concerning this matter, do not hesitate to contact our HR department.</p>\n                    <br/>\n                    Best Regards,\n                    <br/>\n                    <t t-out="user.name"/>\n                    <br/>\n                </div>\n            ':
        '<div style="margin: 0px; padding: 0px;">\n                    <p>عزيزي <t t-out="object.employee_id.name or \'\'"/>,</p>\n                    <p>يرجى الاطلاع على قسيمة الراتب المرفقة للفترة: <t t-out="object.date_from"/> - <t t-out="object.date_to"/></p>\n                    <p>في حال وجود أي استفسارات، لا تتردد في التواصل مع قسم الموارد البشرية.</p>\n                    <br/>\n                    مع التحيات،\n                    <br/>\n                    <t t-out="user.name"/>\n                    <br/>\n                </div>\n            ',

    '<strong>Combined Leave</strong>\n                                    — Annual portion:':
        '<strong>إجازة مدمجة</strong>\n                                    — الجزء السنوي:',
    '<strong>End Date</strong>': '<strong>تاريخ الانتهاء</strong>',
    '<strong>General Manager</strong>': '<strong>المدير العام</strong>',
    '<strong>HR Manager</strong>': '<strong>مدير الموارد البشرية</strong>',
    '<strong>ID No.</strong>': '<strong>رقم الهوية</strong>',
    '<strong>Job Title</strong>': '<strong>المسمى الوظيفي</strong>',
    '<strong>Joining Date</strong>': '<strong>تاريخ الالتحاق</strong>',
    '<strong>Last Return Date</strong>': '<strong>تاريخ آخر عودة</strong>',
    '<strong>Last Wage (SAR)</strong>': '<strong>آخر راتب (ريال)</strong>',
    '<strong>Leave Manager</strong>': '<strong>مدير الإجازة</strong>',
    '<strong>Leave Nature</strong>': '<strong>طبيعة الإجازة</strong>',
    '<strong>Leave Type</strong>': '<strong>نوع الإجازة</strong>',
    '<strong>National / Iqama ID</strong>': '<strong>الهوية الوطنية / الإقامة</strong>',
    '<strong>Overall Status</strong>': '<strong>الحالة الإجمالية</strong>',
    '<strong>Payslip Name</strong>': '<strong>اسم قسيمة الراتب</strong>',
    '<strong>Reference</strong>': '<strong>المرجع</strong>',
    '<strong>Return Date</strong>': '<strong>تاريخ العودة</strong>',
    '<strong>Return Status</strong>': '<strong>حالة العودة</strong>',
    '<strong>Service Years</strong>': '<strong>سنوات الخدمة</strong>',
    '<strong>Start Date</strong>': '<strong>تاريخ البدء</strong>',
    '<strong>Vacation payslip not yet generated.</strong>\n                                    The payslip':
        '<strong>لم تُنشَأ قسيمة راتب الإجازة بعد.</strong>\n                                    قسيمة الراتب',
    "Company bank account used to pay this employee's salary. This is the source account the accounting team debits when processing payroll.":
        'الحساب البنكي للشركة المستخدم لصرف راتب الموظف. هذا هو الحساب المصدر الذي تخصم منه فريق المحاسبة عند معالجة الرواتب.',
    'Early (min)': 'خروج مبكر (دقيقة)',
    'If enabled, only fully absent or unpresented days are deducted. Late arrivals and early departures count as fully worked. Use for employees on fixed monthly salary where partial-day penalties do not apply.':
        'عند التفعيل، تُخصَم فقط أيام الغياب الكاملة أو أيام عدم التسجيل. يُحتسب التأخر والخروج المبكر يوم عمل كامل. يُستخدم للموظفين ذوي الراتب الشهري الثابت حيث لا تُطبَّق غرامات اليوم الجزئي.',
    'The annual leave that triggered the creation of this vacation payslip.  Set automatically by _create_vacation_payslip.':
        'الإجازة السنوية التي تسبَّبت في إنشاء قسيمة راتب الإجازة هذه. تُحدَّد تلقائياً بواسطة _create_vacation_payslip.',
    'The following employees were automatically skipped during\n                    payslip generation. Review and resolve the issues before retrying.':
        'تم تخطي الموظفين التاليين تلقائياً أثناء إنشاء قسائم الرواتب.\n                    راجع المشكلات وحلّها قبل إعادة المحاولة.',
    'The user can read his own payslips and the payslips of every employee below him in the management hierarchy (all direct and indirect reports).':
        'يمكن للمستخدم الاطلاع على قسائم راتبه وقسائم جميع الموظفين التابعين له في التسلسل الإداري (المباشرين وغير المباشرين).',
    'Type of payroll-card operation to include in the file.': 'نوع عملية بطاقة الرواتب المراد تضمينها في الملف.',
    'When enabled, the payslip deducts only for fully absent or unpresented days.  Late arrivals and early departures count as fully worked. Use for employees on fixed monthly salary where partial-day penalties do not apply.':
        'عند التفعيل، تخصم قسيمة الراتب فقط لأيام الغياب الكاملة أو أيام عدم التسجيل. يُحتسب التأخر والخروج المبكر يوم عمل كامل.',
    'Worked Day': 'يوم عمل',
    'days |\n                                    Unpaid portion:':
        'أيام |\n                                    الجزء غير مدفوع الأجر:',
})

# ---- KSW_deduction --------------------------------------------------------
REMAINING.update({
    '<i class="fa fa-check-circle me-1"/>\n                                <strong>Full payment</strong> — will mark all <b>':
        '<i class="fa fa-check-circle me-1"/>\n                                <strong>دفع كامل</strong> — سيُعلَّم جميع <b>',
    '<i class="fa fa-info-circle me-1"/>\n                                <strong>Partial payment</strong>':
        '<i class="fa fa-info-circle me-1"/>\n                                <strong>دفع جزئي</strong>',
    '<i class="fa fa-info-circle" title="Info" role="img" aria-label="Info"/>\n                           ':
        '<i class="fa fa-info-circle" title="معلومة" role="img" aria-label="معلومة"/>\n                           ',
    '<span class="mx-1">/</span>': '<span class="mx-1">/</span>',
    'Active Deductions Only': 'الخصومات الفعّالة فقط',
    'Active Portfolio': 'المحفظة الفعّالة',
    'Amount the employee paid outside the payroll cycle.': 'المبلغ الذي دفعه الموظف خارج دورة الراتب.',
    'Can create and submit loan requests for direct subordinates. Scope limited to own employee + direct reports.':
        'يمكن إنشاء وتقديم طلبات قروض للمرؤوسين المباشرين. النطاق مقتصر على الموظف نفسه والمرؤوسين المباشرين.',
    'Confirm Payment': 'تأكيد الدفع',
    'Create and manage HR-managed non-loan deductions (penalties, salary advances) for all employees. Does not grant access to loans.':
        'إنشاء وإدارة الخصومات غير القرضية التابعة للموارد البشرية (الغرامات، سلف الرواتب) لجميع الموظفين. لا يمنح الوصول إلى القروض.',
    'Current contract wage of the employee.': 'راتب الموظف الحالي بموجب العقد.',
    'Deductions This Month': 'خصومات هذا الشهر',
    'Disbursement Confirmed By': 'أُكِّد صرف القرض بواسطة',
    'Disbursement Confirmed Date': 'تاريخ تأكيد صرف القرض',
    'Employee loan account number in BAS (bank loan/financing system).': 'رقم حساب قرض الموظف في BAS (نظام القروض البنكية).',
    'Forwarded From Payslip': 'منقول من قسيمة الراتب',
    'HR Department': 'قسم الموارد البشرية',
    'HR Officer (Non-Loan)': 'مسؤول موارد بشرية (غير قروض)',
    'Installment Schedule': 'جدول الأقساط',
    'Is Full Payment': 'دفع كامل',
    'Loan Acc No. in BAS': 'رقم حساب القرض في BAS',
    'Loan Acc No. in Bas': 'رقم حساب القرض في BAS',
    'Loan Disbursement Officer': 'مسؤول صرف القرض',
    'Lower is collected first when salary is insufficient (used to order deduction collection at payroll time).':
        'الأولوية الأدنى تُحصَّل أولاً عند عدم كفاية الراتب (لترتيب تحصيل الخصومات وقت الرواتب).',
    'Lower is collected first when salary is insufficient.': 'الأولوية الأدنى تُحصَّل أولاً عند عدم كفاية الراتب.',
    'Managed By': 'تُدار بواسطة',
    'Manual Payments': 'المدفوعات اليدوية',
    'Mark Paid': 'تسجيل كمدفوع',
    'Mark this installment as settled outside payroll': 'تسجيل هذا القسط كمُسوَّى خارج الراتب',
    'New Amount / Installment': 'المبلغ الجديد / القسط',
    'Order in which this deduction type is collected when the employee salary cannot cover all deductions.':
        'ترتيب جمع هذا النوع من الخصومات عندما لا يكفي راتب الموظف لتغطية جميع الخصومات.',
    'Original installment this pending remainder was split from.': 'القسط الأصلي الذي تفرَّعت منه هذه البقية المعلَّقة.',
    'Outstanding Balance': 'الرصيد المتبقي',
    'Outstanding Before Payment': 'المتبقي قبل الدفع',
    'Overdue': 'متأخر',
    'Payment Amount': 'مبلغ الدفع',
    'Payment Details': 'تفاصيل الدفع',
    'Payroll Impact': 'الأثر على الراتب',
    'Payroll Priority': 'أولوية الراتب',
    'Payslip whose shortfall created/forwarded this pending line.': 'قسيمة الراتب التي أنشأت/نقلت هذا البند المعلَّق.',
    'Pending Disbursement': 'بانتظار الصرف',
    'Pending Installments': 'الأقساط المعلَّقة',
    'Pending My Action': 'بانتظار إجراء مني',
    'Pending installment whose scheduled month has already passed.': 'قسط معلَّق تجاوز شهره المجدول.',
    'Pending installments will each be reduced to this amount.': 'سيتم تخفيض كل قسط معلَّق إلى هذا المبلغ.',
    'Receipt #, bank reference…': 'رقم الإيصال، مرجع بنكي…',
    'Receipt number, bank reference, or any other identifier.': 'رقم الإيصال، مرجع بنكي، أو أي معرّف آخر.',
    'Record Loan Payment': 'تسجيل دفعة قرض',
    'Record Payment': 'تسجيل دفعة',
    'Reference / Note': 'المرجع / الملاحظة',
    'Remaining After Payment': 'المتبقي بعد الدفع',
    'Result': 'النتيجة',
    'SSN No.': 'رقم الضمان الاجتماعي',
    'Social Security Number': 'رقم الضمان الاجتماعي',
    'Split Origin': 'مصدر التجزئة',
    'Total Deductions This Month': 'إجمالي الخصومات هذا الشهر',
    'True if the current user may add manual installment lines or reschedule pending installments on this deduction.':
        'صحيح إذا كان المستخدم الحالي مخوَّلاً إضافة بنود أقساط يدوية أو إعادة جدولة الأقساط المعلَّقة على هذا الخصم.',
    'True when payment_amount covers the full outstanding balance.': 'صحيح عندما يغطي مبلغ الدفع كامل الرصيد المتبقي.',
    'Waiting For Me': 'بانتظاري',
    'Which department can manually close (mark as paid) deductions of this type outside payroll. HR: gov. penalties, advances. Accounting: loans.':
        'القسم الذي يمكنه إغلاق (تسجيل كمدفوع) الخصومات من هذا النوع يدوياً خارج الراتب. الموارد البشرية: الغرامات الحكومية والسلف. المحاسبة: القروض.',
    'X Can Edit Installments': 'يمكن تعديل الأقساط',
    'X Can Select Loan Type': 'يمكن اختيار نوع القرض',
    'installment(s):\n                                each becomes':
        'قسط/أقساط:\n                                كل منها يصبح',
    'pending installment(s) will be marked as paid.': 'قسط/أقساط معلَّق سيُعلَّم كمدفوع.',
    'will be redistributed across': 'سيُوزَّع على',
    '✅ Confirm Disbursement': '✅ تأكيد الصرف',
})

# ---- KSW_annual_leave additional strings -----------------------------------
REMAINING.update({
    'Pending My Action': 'بانتظار إجراء مني',
    'Waiting For Me': 'بانتظاري',
    '✅ Confirm & Finalise': '✅ تأكيد وإنهاء',
})

# ---- KSW_attendance_leave additional strings --------------------------------
REMAINING.update({
    'If enabled, only a check-in punch is required. Early-leave deductions are suppressed for this type.':
        'عند التفعيل، يُكتفى بتسجيل الدخول فقط. يتم تجاهل خصومات الخروج المبكر لهذا النوع.',
    'Set when a stale-sync alert email has been sent for the current outage; cleared automatically when the device syncs again.':
        'يُحدَّد عند إرسال بريد تنبيه لانقطاع مزامنة الجهاز؛ يُمسح تلقائياً عند استئناف المزامنة.',
})

# ---- KSW_attendance_sheet additional strings --------------------------------
REMAINING.update({
    'False once the sheet is confirmed/locked, or — for supervisors — once it is no longer within the editable window.':
        'خطأ عند تأكيد الكشف أو قفله، أو — للمشرفين — عند خروجه من نافذة التعديل.',
    "If checked, this employee's attendance is managed via the monthly attendance sheet (manual entry by supervisor). Biometric punch records are still synced but treated as reference only.":
        'عند التفعيل، تُدار بيانات حضور الموظف عبر كشف الحضور الشهري (إدخال يدوي من قِبَل المشرف). لا تزال سجلات البصمة البيومترية تُزامَن ولكنها تُستخدم كمرجع فقط.',
})

# ---- KSW_eos_leave additional strings --------------------------------------
REMAINING.update({
    '<span class="o_stat_text">Recompute</span>\n                        <span class="o_stat_text">EOS Payslip</span>':
        '<span class="o_stat_text">إعادة احتساب</span>\n                        <span class="o_stat_text">قسيمة نهاية الخدمة</span>',
    'This will cancel the existing EOS payslip and recreate it from the current leave inputs. Continue?':
        'سيؤدي هذا إلى إلغاء قسيمة نهاية الخدمة الحالية وإعادة إنشائها من مدخلات الإجازة الحالية. هل تريد المتابعة؟',
})

# ---- KSW_unpaid_leave additional strings -----------------------------------
REMAINING.update({
    '___________________________': '___________________________',
})

# ---- KSW_working_schedule additional strings --------------------------------
REMAINING.update({
    'When set, Saturday is a short shift (<8h). The gap between a full 8h day and the actual scheduled hours is deducted from the employee\'s payslip and then credited back as a Saturday overtime credit — resulting in a net-zero pay impact while correctly classifying the time.':
        'عند التفعيل، يكون السبت وردية قصيرة (أقل من 8 ساعات). يُخصَم الفرق بين يوم العمل الكامل (8 ساعات) والساعات المجدولة الفعلية من قسيمة الراتب ثم يُردُّ كرصيد إضافي للسبت — مما يُحقق تأثيراً صفرياً على الأجر مع التصنيف الصحيح للوقت.',
    'When set, Saturday is treated as a required workday for attendance-sheet purposes. Absence on Saturday is penalised the same as any regular weekday.':
        'عند التفعيل، يُعامَل السبت كيوم عمل مطلوب لأغراض كشف الحضور. يُعاقَب الغياب يوم السبت بنفس طريقة أيام العمل العادية.',
    'When set, late-arrival and early-departure minutes are not recorded for employees on this schedule. Useful for management-level or flexible-hour positions.':
        'عند التفعيل، لا تُسجَّل دقائق التأخر والخروج المبكر للموظفين على هذا الجدول. مفيد لوظائف المستوى الإداري أو المرونة في ساعات العمل.',
})

# ---- KSW_commissions — simple strings ----------------------------------------
REMAINING.update({
    '0 means no upper bound.': '0 يعني بلا حد أعلى.',
    'A salesperson can only have one profile per year.': 'يمكن أن يكون للمندوب ملف واحد فقط في السنة.',
    'A technician can only appear once per location-allowance sheet.': 'يمكن أن يظهر الفني مرة واحدة فقط في كل كشف بدل الموقع.',
    'Account no. as in col 0 of the Excel Sales file (primary match key)': 'رقم الحساب كما في العمود 0 من ملف Excel للمبيعات (مفتاح المطابقة الأساسي)',
    "Account number exactly as it appears in col 0 of the accountant's monthly Sales Excel file. Used by the import wizard as the primary key to match this customer to the correct commission split bucket. Takes priority over the Commission Import Name.":
        'رقم الحساب كما يظهر في العمود 0 من ملف Excel الشهري للمبيعات. يستخدمه معالج الاستيراد كمفتاح أساسي لمطابقة هذا العميل بالمجموعة الصحيحة. له الأولوية على اسم استيراد العمولة.',
    'Achieved Amount (auto)': 'المبلغ المحقَّق (تلقائي)',
    'Achieved Collection': 'التحصيل المحقَّق',
    'Achieved Sales': 'المبيعات المحقَّقة',
    'Allowances &amp; Commissions': 'البدلات والعمولات',
    'Amount Formula': 'صيغة المبلغ',
    'Annual Collection Target': 'هدف التحصيل السنوي',
    'Annual Sales Target': 'هدف المبيعات السنوي',
    'Applies To': 'ينطبق على',
    'Apply Override': 'تطبيق التجاوز',
    'Auto-resolved combined sales+collection commission for hybrid (Salesman & Collector) employees.':
        'العمولة المشتركة للمبيعات والتحصيل المحسوبة تلقائياً للموظفين المختلطين (مندوب ومحصِّل).',
    'Auto-resolved from the confirmed sales-commission line for this employee/period.':
        'يُحدَّد تلقائياً من بند عمولة المبيعات المؤكَّد لهذا الموظف/الفترة.',
    'Auto-resolved from the matching technician location-allowance line (per period). Updated when the location-allowance sheet is confirmed.':
        'يُحدَّد تلقائياً من بند بدل الموقع المطابق للفني (لكل فترة). يتم تحديثه عند تأكيد كشف بدل الموقع.',
    'Bank<br/>Transfer': 'تحويل<br/>بنكي',
    'Base': 'الأساس',
    'Both': 'كلاهما',
    'Breakfast': 'الإفطار',
    'Breakfast (SAR)': 'الإفطار (ريال)',
    'Breakfast (qty)': 'الإفطار (عدد)',
    'Breakfast Amount': 'مبلغ الإفطار',
    'Breakfast Price': 'سعر الإفطار',
    'Breakfast Qty': 'عدد الإفطار',
    'Brief justification for granting the commission despite the unmet condition. Stored on the line and posted to the sheet chatter for audit.':
        'مبرر مختصر لمنح العمولة رغم عدم استيفاء الشرط. يُحفَظ على البند وينشر في سجل الكشف للمراجعة.',
    'Client': 'العميل',
    'Client Account Number': 'رقم حساب العميل',
    'Client Split': 'تقسيم العميل',
    'Client Splits': 'تقسيمات العملاء',
    'Client-specific': 'خاص بعميل',
    'Clients': 'العملاء',
    'Coll. %': 'نسبة التحصيل ٪',
    'Coll. Comm.': 'عمولة التحصيل',
    'Coll. Rule': 'قاعدة التحصيل',
    'Collection': 'التحصيل',
    'Collection %': 'نسبة التحصيل ٪',
    'Collection Achieved': 'التحصيل المحقَّق',
    'Collection Based on Total': 'التحصيل بناءً على الإجمالي',
    'Collection Commission Amount': 'مبلغ عمولة التحصيل',
    'Collection Commission Rule': 'قاعدة عمولة التحصيل',
    'Collection Excel File': 'ملف Excel للتحصيل',
    'Collection File': 'ملف التحصيل',
    'Collection File Name': 'اسم ملف التحصيل',
    'Collection Manager Options': 'خيارات مدير التحصيل',
    'Collection Min %': 'الحد الأدنى لنسبة التحصيل ٪',
    'Collection Rule': 'قاعدة التحصيل',
    'Collection Target': 'هدف التحصيل',
    'Collector': 'المحصِّل',
    'Combined': 'مشترك',
    'Combined (Sales & Collection)': 'مشترك (مبيعات وتحصيل)',
    'Combined (sales + collection) rule for this employee. If blank, auto-selected by scope matching.':
        'قاعدة مشتركة (مبيعات + تحصيل) لهذا الموظف. إذا كانت فارغة، يتم اختيارها تلقائياً.',
    'Combined Comm.': 'العمولة المشتركة',
    'Combined Commission': 'العمولة المشتركة',
    'Combined Commission Amount': 'مبلغ العمولة المشتركة',
    'Combined Commission Rule': 'قاعدة العمولة المشتركة',
    'Combined Rule': 'القاعدة المشتركة',
    'Commission Import Name': 'اسم استيراد العمولة',
    'Commission Rule': 'قاعدة العمولة',
    'Commission Rules': 'قواعد العمولات',
    'Commission Type': 'نوع العمولة',
    'Commission ratio applied to the base amount (target or achieved). E.g. 1.5 means 1.5% of base.':
        'نسبة العمولة المطبَّقة على المبلغ الأساسي (المستهدف أو المحقَّق). مثلاً: 1.5 تعني 1.5٪ من الأساس.',
    'Commission type applied to this client bucket (sales-only is the most common for special-client splits).':
        'نوع العمولة المطبَّق على مجموعة العميل هذه (المبيعات فقط هو الأكثر شيوعاً لتقسيمات العملاء الخاصين).',
    'Commissions & Allowances Summary': 'ملخص العمولات والبدلات',
    'Commissions &amp; Allowances Summary': 'ملخص العمولات والبدلات',
    'Computed Amounts': 'المبالغ المحسوبة',
    'Condition': 'الشرط',
    'Condition Formula': 'صيغة الشرط',
    'Condition Overridden': 'تم تجاوز الشرط',
    'Condition Type': 'نوع الشرط',
    'Custom Formula (advanced)': 'صيغة مخصصة (متقدم)',
    'Customer name as in col 1 of the Excel Sales file (fallback if account# blank)':
        'اسم العميل كما في العمود 1 من ملف Excel للمبيعات (بديل عند عدم وجود رقم حساب)',
    "Customer name exactly as it appears in the accountant's monthly Sales Excel file (col 1 = Customer name). Fallback when Commission Account Number is blank. Leave blank to fall back to the partner's regular Name.":
        'اسم العميل كما يظهر في العمود 1 من ملف Excel الشهري للمبيعات. يُستخدم بديلاً عند عدم وجود رقم الحساب. اتركه فارغاً للرجوع إلى الاسم المعتاد للشريك.',
    'Days, Trips, Hours…': 'أيام، رحلات، ساعات…',
    'Default quantity pre-filled on new sheets when the category is Quantity-Based. The supervisor can adjust it on each sheet.':
        'الكمية الافتراضية المُعبَّأة مسبقاً على الكشوف الجديدة عندما تكون الفئة قائمة على الكمية. يمكن للمشرف تعديلها على كل كشف.',
    'Defaults to the salesperson profile role for the year. May be overridden per line if needed.':
        'يُعيَّن افتراضياً من دور ملف المندوب للسنة. يمكن تجاوزه لكل بند إذا لزم الأمر.',
    'Dinner': 'العشاء',
    'Dinner (SAR)': 'العشاء (ريال)',
    'Dinner (qty)': 'العشاء (عدد)',
    'Dinner Amount': 'مبلغ العشاء',
    'Dinner Price': 'سعر العشاء',
    'Dinner Qty': 'عدد العشاء',
    'Display label for the quantity field on the sheet line (e.g. "Days", "Trips", "Hours"). Falls back to "Quantity" when empty.':
        'تسمية حقل الكمية على بند الكشف (مثل "أيام"، "رحلات"، "ساعات"). يرجع إلى "الكمية" عند تركه فارغاً.',
    'Driver<br/>Commission': 'عمولة<br/>السائق',
    'Drives which kind of commission rules apply: sales, collection, or combined.':
        'يحدد نوع قواعد العمولات المطبَّقة: مبيعات، أو تحصيل، أو مشترك.',
    'Dual Threshold (sales% AND collection% both ≥ X%)': 'حد مزدوج (نسبة المبيعات ونسبة التحصيل كلاهما ≥ X٪)',
    'Each month can only appear once per profile.': 'يمكن أن يظهر كل شهر مرة واحدة فقط في الملف.',
    'Employee + Client': 'الموظف + العميل',
    'Employee-specific': 'خاص بموظف',
    'Exclude VAT (÷ 1.15)': 'استبعاد ضريبة القيمة المضافة (÷ 1.15)',
    'Factor applied to Rate % when achievement is in the reduced zone (≤ Reduced Rate Up To %). E.g. 0.7 = 70 %% of the normal rate. Has no effect when Reduced Rate Up To % is 0.':
        'المعامل المطبَّق على نسبة العمولة عندما يكون الإنجاز في المنطقة المخفَّضة. مثلاً: 0.7 = 70٪ من النسبة العادية. لا تأثير عندما يكون الحد الأعلى للمنطقة المخفَّضة 0.',
    'From %': 'من ٪',
    'General (default)': 'عام (افتراضي)',
    'General Settings': 'الإعدادات العامة',
    'Grant commission exception (Sales Manager)': 'منح استثناء العمولة (مدير المبيعات)',
    'Gross<br/>Total': 'الإجمالي<br/>قبل الخصم',
    'Import': 'استيراد',
    'Import Sales & Collection from Excel': 'استيراد المبيعات والتحصيل من Excel',
    "Import achieved sales & collection amounts from the accountant's Excel files":
        'استيراد مبالغ المبيعات والتحصيل المحقَّقة من ملفات Excel للمحاسب',
    'Import from Excel': 'استيراد من Excel',
    'Internal notes — when does this rule apply, why was it created…':
        'ملاحظات داخلية — متى تنطبق هذه القاعدة، لماذا أُنشئت…',
    'KSW Commissions Settings': 'إعدادات عمولات KSW',
    'KSW Sales / Collection Commission Line': 'بند عمولة مبيعات / تحصيل KSW',
    'KSW Sales / Collection Commission Rule': 'قاعدة عمولة مبيعات / تحصيل KSW',
    'KSW Sales / Collection Commission Sheet': 'كشف عمولة مبيعات / تحصيل KSW',
    'KSW Sales / Collection Commission Tier': 'شريحة عمولة مبيعات / تحصيل KSW',
    'KSW Sales/Collection Commission Import Wizard': 'معالج استيراد عمولة مبيعات / تحصيل KSW',
    'KSW Salesperson Monthly Target': 'هدف المندوب الشهري KSW',
    'KSW Salesperson Profile (yearly target)': 'ملف المندوب (الهدف السنوي) KSW',
    'KSW Salesperson Profile — Client Split Rule': 'ملف المندوب — قاعدة تقسيم العميل KSW',
    'KSW Technician Location Allowance Line': 'بند بدل موقع الفني KSW',
    'KSW Technician Location Allowance Sheet': 'كشف بدل موقع الفني KSW',
    'Label': 'التسمية',
    'Lines<br/>Subtotal': 'مجموع<br/>البنود',
    'Live unit price from KSW_commissions general settings.': 'سعر الوحدة الحالي من الإعدادات العامة لعمولات KSW.',
    'Loans<br/>Deduction': 'خصم<br/>القروض',
    'Location Allowance Amount': 'مبلغ بدل الموقع',
    'Location Allowance Sheets': 'كشوف بدل الموقع',
    'Lunch': 'الغداء',
    'Lunch (SAR)': 'الغداء (ريال)',
    'Lunch (qty)': 'الغداء (عدد)',
    'Lunch Amount': 'مبلغ الغداء',
    'Lunch Price': 'سعر الغداء',
    'Lunch Qty': 'عدد الغداء',
    'Max Quantity': 'أقصى كمية',
    'Maximum quantity accepted on a sheet line. 0 means no upper bound (e.g. unlimited Fridays).':
        'الحد الأقصى للكمية المقبولة في بند الكشف. 0 تعني بلا حد أعلى (مثل: أيام جمعة غير محدودة).',
    'Meal Occurrences': 'عدد الوجبات',
    'Min Quantity': 'أدنى كمية',
    'Minimum achievement percentage on collection for the condition to pass (used by single_threshold when metric=collection, and by dual_threshold).':
        'الحد الأدنى لنسبة إنجاز التحصيل لاجتياز الشرط.',
    'Minimum achievement percentage on sales for the condition to pass (used by single_threshold when metric=sales or kind=combined, and by dual_threshold).':
        'الحد الأدنى لنسبة إنجاز المبيعات لاجتياز الشرط.',
    'Minimum quantity accepted on a sheet line. 0 = no lower bound (negative quantities are still rejected).':
        'الحد الأدنى للكمية المقبولة في بند الكشف. 0 = بلا حد أدنى (لا تزال الكميات السالبة مرفوضة).',
    'Mirrored from the category. Drives readonly on Amount and shows the Quantity column.':
        'مأخوذ من الفئة. يتحكم في قراءة المبلغ ويظهر عمود الكمية.',
    'Monthly Sheets': 'الكشوف الشهرية',
    'Monthly Targets': 'الأهداف الشهرية',
    "Name exactly as it appears in the accountant's monthly Sales / Collection Excel files (column \"البائع\" / \"مندوب التحصيل\"). Used by the Excel import wizard to auto-match rows to this employee. Leave blank to fall back to the employee's regular name.":
        'الاسم كما يظهر في ملفات Excel الشهرية للمبيعات/التحصيل (عمود "البائع" / "مندوب التحصيل"). يستخدمه معالج الاستيراد للمطابقة التلقائية. اتركه فارغاً للرجوع إلى الاسم المعتاد.',
    'Names of clients from the rule, shown for quick reference.': 'أسماء العملاء من القاعدة، تظهر للرجوع السريع.',
    'No sheets selected.': 'لم يتم اختيار أي كشوف.',
    'One or more clients this rule applies to. Leave empty when scope is General or Employee-specific.':
        'عميل واحد أو أكثر تنطبق عليهم هذه القاعدة. اتركه فارغاً عندما يكون النطاق عاماً أو خاصاً بموظف.',
    'Only one sales/collection commission sheet per month.': 'كشف عمولة مبيعات/تحصيل واحد فقط في الشهر.',
    'Only one technician location-allowance sheet per month.': 'كشف بدل موقع فني واحد فقط في الشهر.',
    'Optional. When set, client-specific commission rules will be matched first by the resolver.':
        'اختياري. عند التحديد، ستُطابَق قواعد العمولات الخاصة بالعميل أولاً.',
    'Optional: when the achievement % is at or below this value the rate is multiplied by the Reduced Rate Multiplier. Leave at 0 to always use the full Rate %.':
        'اختياري: عندما تكون نسبة الإنجاز عند هذه القيمة أو أقل منها، تُضرَب النسبة في معامل النسبة المخفَّضة. اتركه 0 لاستخدام النسبة الكاملة دائماً.',
    'Overridden By': 'تم التجاوز بواسطة',
    'Override': 'تجاوز',
    'Override Commission Condition': 'تجاوز شرط العمولة',
    'Override Condition': 'تجاوز الشرط',
    'Override Date': 'تاريخ التجاوز',
    'Override Reason': 'سبب التجاوز',
    'Override the commission-rule condition on individual sales/collection commission lines (e.g. grant the commission to a salesperson who fell below the threshold).':
        'تجاوز شرط قاعدة العمولة على بنود عمولة المبيعات/التحصيل الفردية (مثلاً: منح العمولة لمندوب لم يبلغ الحد الأدنى).',
    'Overwrite the 12 monthly target rows with an even split of the current annual totals?':
        'هل تريد استبدال الأهداف الشهرية الـ12 بتوزيع متساوٍ للإجماليات السنوية الحالية؟',
    'Price (SAR) per breakfast occurrence.': 'السعر (ريال) لكل وجبة إفطار.',
    'Price (SAR) per dinner occurrence.': 'السعر (ريال) لكل وجبة عشاء.',
    'Price (SAR) per lunch occurrence.': 'السعر (ريال) لكل وجبة غداء.',
    'Priority': 'الأولوية',
    'Profile': 'الملف',
    'Python expression assigning a boolean to ``result``. Variables in scope: ``sales_pct``, ``collection_pct``, ``sales_target``, ``collection_target``, ``sales_achieved``, ``collection_achieved``, ``employee``, ``client``.\nExample: result = sales_pct >= 50 and collection_pct >= 60':
        'تعبير Python يُسنِد قيمة منطقية إلى ``result``. المتغيرات المتاحة: ``sales_pct``، ``collection_pct``، ``sales_target``، ``collection_target``، ``sales_achieved``، ``collection_achieved``، ``employee``، ``client``.\nمثال: result = sales_pct >= 50 and collection_pct >= 60',
    'Python expression evaluated to compute the line amount from the entered quantity. The variable ``quantity`` (alias ``qty``) is in scope; assign the final amount to ``result``.\n\nExample: result = quantity * 100\nExample: result = qty * 50 + (100 if qty >= 5 else 0)':
        'تعبير Python يُحسَب لاستخراج مبلغ البند من الكمية المُدخَلة. المتغير ``quantity`` (اسم مستعار ``qty``) متاح؛ أسنِد المبلغ النهائي إلى ``result``.\n\nمثال: result = quantity * 100\nمثال: result = qty * 50 + (100 if qty >= 5 else 0)',
    'Qty': 'الكمية',
    'Quantity': 'الكمية',
    'Quantity Label': 'تسمية الكمية',
    'Quantity-Based Amount': 'مبلغ قائم على الكمية',
    'Rate %': 'نسبة العمولة ٪',
    'Redistribute Annual → Monthly (Even)': 'إعادة توزيع السنوي → الشهري (متساوٍ)',
    'Reduced Rate Multiplier': 'معامل النسبة المخفَّضة',
    'Reduced Rate Up To %': 'النسبة المخفَّضة حتى ٪',
    'Resolved Rules': 'القواعد المحلولة',
    'Revoke': 'إلغاء',
    'Revoke Override': 'إلغاء التجاوز',
    'Revoke override': 'إلغاء التجاوز',
    'Role': 'الدور',
    'Rule': 'القاعدة',
    'Rule for this client group. Its Clients list (partner_ids) defines which customers belong to this split bucket. Must have scope "Client-specific" or "Employee + Client".':
        'القاعدة الخاصة بمجموعة العميل هذه. قائمة العملاء تحدد العملاء في هذه المجموعة. يجب أن يكون النطاق "خاص بعميل" أو "الموظف + العميل".',
    'Rule used to calculate collection commission for this employee. If blank, auto-selected by scope matching.':
        'القاعدة المستخدمة لحساب عمولة التحصيل لهذا الموظف. إذا كانت فارغة، يتم اختيارها تلقائياً.',
    'Rule used to calculate sales commission for this employee. If blank, the system auto-selects the most specific active rule that matches this employee (scope matching).':
        'القاعدة المستخدمة لحساب عمولة المبيعات لهذا الموظف. إذا كانت فارغة، يختار النظام تلقائياً القاعدة الأكثر تحديداً.',
    'Sales': 'المبيعات',
    'Sales %': 'نسبة المبيعات ٪',
    'Sales & Collection Commissions': 'عمولات المبيعات والتحصيل',
    'Sales / Collection Commission Rule': 'قاعدة عمولة مبيعات / تحصيل',
    'Sales / Collection Commission Sheet': 'كشف عمولة مبيعات / تحصيل',
    'Sales Achieved': 'المبيعات المحقَّقة',
    'Sales Comm.': 'عمولة المبيعات',
    'Sales Commission': 'عمولة المبيعات',
    'Sales Commission Amount': 'مبلغ عمولة المبيعات',
    'Sales Commission Rule': 'قاعدة عمولة المبيعات',
    'Sales Excel File': 'ملف Excel للمبيعات',
    'Sales File': 'ملف المبيعات',
    'Sales File Name': 'اسم ملف المبيعات',
    'Sales Manager': 'مدير المبيعات',
    'Sales Min %': 'الحد الأدنى لنسبة المبيعات ٪',
    'Sales Override': 'تجاوز المبيعات',
    'Sales Rule': 'قاعدة المبيعات',
    'Sales Target': 'هدف المبيعات',
    'Sales-Manager Commission Condition Override Wizard': 'معالج تجاوز شرط عمولة مدير المبيعات',
    'Sales/Collection Commission Line': 'بند عمولة مبيعات / تحصيل',
    'Sales/Collection Commission Rules': 'قواعد عمولة مبيعات / تحصيل',
    'Sales/Collection Commission Sheets': 'كشوف عمولة مبيعات / تحصيل',
    'Salesman': 'البائع',
    'Salesman & Collector': 'بائع ومحصِّل',
    'Salesperson': 'المندوب',
    'Salesperson Profile': 'ملف المندوب',
    'Salesperson Profiles': 'ملفات المندوبين',
    'Scope': 'النطاق',
    "Set by a Sales Manager via the \"Override Condition\" button. While True, the commission is paid even when the rule's condition (threshold / formula) does not pass — using the tier ladder applied to the actual achievement percentage (or the lowest tier as a floor).":
        'يُحدَّد من قِبَل مدير المبيعات عبر زر "تجاوز الشرط". عندما يكون مُفعَّلاً، تُدفَع العمولة حتى لو لم يُستوفَ شرط القاعدة — باستخدام سلّم الشرائح مطبَّقاً على نسبة الإنجاز الفعلية.',
    'Short description of this bucket, e.g. "Special Clients — Sales Only".':
        'وصف مختصر لهذه المجموعة، مثل "عملاء خاصون — مبيعات فقط".',
    'Single Threshold (one metric ≥ X%)': 'حد أحادي (مقياس واحد ≥ X٪)',
    'Split': 'تقسيم',
    'Target Amount (auto)': 'المبلغ المستهدف (تلقائي)',
    'Target Collection': 'هدف التحصيل',
    'Target Line': 'بند الهدف',
    'Target Sales': 'هدف المبيعات',
    'Targets & Achieved': 'الأهداف والمحقَّق',
    'Technician': 'الفني',
    'Technician Allowance': 'بدل الفني',
    'Technician Location Allowance': 'بدل موقع الفني',
    'Technicians': 'الفنيون',
    'Threshold Metric': 'مقياس الحد الأدنى',
    'Tie-breaker within the same scope. Lower priority wins. Use this to layer multiple employee-specific rules.':
        'الفاصل عند تساوي الأولويات ضمن النطاق ذاته. الأولوية الأدنى تفوز. استخدمه لتطبيق قواعد متعددة خاصة بموظف.',
    'Tier Ladder': 'سلّم الشرائح',
    'To %': 'إلى ٪',
    'Total Allowance': 'إجمالي البدلات',
    'Total Collection Commission': 'إجمالي عمولة التحصيل',
    'Total Combined Commission': 'إجمالي العمولة المشتركة',
    'Total Sales Commission': 'إجمالي عمولة المبيعات',
    'Unit price (SAR) per breakfast occurrence on a technician location-allowance line.':
        'سعر وحدة الإفطار (ريال) على بند بدل موقع الفني.',
    'Unit price (SAR) per dinner occurrence on a technician location-allowance line.':
        'سعر وحدة العشاء (ريال) على بند بدل موقع الفني.',
    'Unit price (SAR) per lunch occurrence on a technician location-allowance line.':
        'سعر وحدة الغداء (ريال) على بند بدل موقع الفني.',
    'Unit prices used by the Technician Location Allowance calculator. Changing these values updates every line on every existing sheet on next read.':
        'أسعار الوحدات المستخدمة في حاسبة بدل موقع الفني. تحديث هذه القيم يُحدِّث جميع البنود عند القراءة التالية.',
    'Upper bound (inclusive). Leave at 0 for an unbounded top tier.':
        'الحد الأعلى (شامل). اتركه 0 لشريحة مفتوحة النهاية.',
    'Used when the category is Quantity-Based. The line amount is computed by the category formula from this quantity.':
        'يُستخدم عندما تكون الفئة قائمة على الكمية. يُحسَب مبلغ البند بصيغة الفئة من هذه الكمية.',
    'When checked, both the Target and Collected amounts from the collection file are divided by 1.15 to remove the 15% VAT before being written to the commission line.':
        'عند التفعيل، يُقسَم كل من الهدف والمبلغ المحصَّل من ملف التحصيل على 1.15 لاستبعاد ضريبة القيمة المضافة 15٪ قبل كتابتهما في بند العمولة.',
    'When checked, sheet lines using this category disable the "Amount" field and compute it from a Python formula applied to the entered quantity.':
        'عند التفعيل، تُعطَّل حقل "المبلغ" في بنود الكشف لهذه الفئة ويُحسَب من صيغة Python مطبَّقة على الكمية المُدخَلة.',
    "When enabled, the Excel import sets this employee's Achieved Collection to the grand total of ALL collections across every rep in the Excel file.":
        'عند التفعيل، يُعيِّن استيراد Excel التحصيل المحقَّق لهذا الموظف إلى إجمالي كل التحصيلات عبر جميع المندوبين في الملف.',
    'When set, this line covers only the clients defined in the split rule. The general line (split blank) receives the remaining totals.':
        'عند التحديد، يغطي هذا البند فقط العملاء المحددين في قاعدة التقسيم. البند العام (بدون تقسيم) يتلقى الإجماليات المتبقية.',
    'Which metric the single threshold is checked against.': 'المقياس الذي يُقارَن به الحد الأحادي.',
    'e.g. Approved by Sales Manager — major customer demobilised mid-month due to project delay; achievement was 63% vs the 65% threshold.':
        'مثال: موافقة مدير المبيعات — عميل رئيسي توقَّف منتصف الشهر بسبب تأخر المشروع؛ الإنجاز كان 63٪ مقابل حد 65٪.',
    'e.g. Sales — General': 'مثال: المبيعات — عام',
    'result = quantity * 100': 'result = quantity * 100',
    'result = sales_pct >= 50 and collection_pct >= 60': 'result = sales_pct >= 50 and collection_pct >= 60',
    '✓ Done': '✓ تم',
    '× Price': '× السعر',
})

# ---- Some large HTML/complex strings from KSW_commissions ------------------
REMAINING.update({
    'Controls which monetary amount the Rate %% is applied to AND how the tier ladder is walked.\n\nTARGET bases (Target / Sales Target / Collection Target):\n  Walk the ladder using the real achievement %%. A tier is only reached once %%achievement exceeds its lower bound.\n  Slice = band_pts / 100 × target.\n  → Use when commission is proportional to the target amount.\n\nACHIEVED bases (Achieved / Sales Achieved / Collection Achieved):\n  Walk ALL tiers unconditionally — the earned amount is split proportionally across every tier band.\n  Tier 1 (0-70%%) always gets 70%% of achieved; Tier 2 (70-∞) gets the rest — regardless of %%achievement.\n  → Use when commission is proportional to the actual earned amount.\n\n• "(auto)" variants use the rule\'s own metric.\n• Explicit "Sales …" / "Collection …" are useful on Combined rules.':
        'يتحكم في المبلغ الذي تُطبَّق عليه نسبة العمولة وكيفية المرور على سلّم الشرائح.\n\nقواعد الهدف (الهدف / هدف المبيعات / هدف التحصيل):\n  يُمشى على السلّم بنسبة الإنجاز الفعلية. لا تُبلَغ الشريحة إلا عند تجاوز حدها الأدنى.\n  الشريحة = نقاط_الشريحة / 100 × الهدف.\n  → استخدم عندما تكون العمولة متناسبة مع الهدف.\n\nقواعد المحقَّق (المحقَّق / المبيعات المحقَّقة / التحصيل المحقَّق):\n  يُمشى على جميع الشرائح دون شرط — يوزَّع المبلغ المكتسب على شرائح بالتناسب.\n  → استخدم عندما تكون العمولة متناسبة مع المبلغ الفعلي.',
})


def fill_remaining(po_path, translations):
    """Fill empty msgstr entries from translations dict and save in place."""
    if not os.path.exists(po_path):
        print(f'skip (missing): {po_path}')
        return 0
    po = polib.pofile(po_path)
    filled = 0
    for entry in po:
        if entry.obsolete or entry.msgstr or entry.msgid_plural:
            continue
        # Try exact match first
        ar = translations.get(entry.msgid)
        if ar:
            entry.msgstr = ar
            filled += 1
        else:
            # Try truncated match for very long strings
            for key, val in translations.items():
                if len(key) > 80 and entry.msgid.startswith(key[:80]):
                    entry.msgstr = val
                    filled += 1
                    break
    po.save(po_path)
    return filled


def main():
    modules = [
        'KSW_payroll',
        'KSW_deduction',
        'KSW_annual_leave',
        'KSW_attendance_leave',
        'KSW_attendance_sheet',
        'KSW_eos_leave',
        'KSW_unpaid_leave',
        'KSW_working_schedule',
        'KSW_commissions',
    ]
    for mod in modules:
        path = f'{BASE}/{mod}/i18n/ar_001.po'
        n = fill_remaining(path, REMAINING)
        if n:
            print(f'{mod}: filled {n} additional entries')
        # Report what's left
        if os.path.exists(path):
            po = polib.pofile(path)
            still = sum(1 for e in po if not e.msgstr and not e.obsolete and not e.msgid_plural)
            if still:
                print(f'  → {still} entries still empty')


if __name__ == '__main__':
    main()
