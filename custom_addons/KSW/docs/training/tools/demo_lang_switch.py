# -*- coding: utf-8 -*-
"""
Flip the staged helpdesk demo tickets between English and Arabic wording, so
the Arabic screenshots show an Arabic screen (ticket subjects and bodies are
plain Char/Html data, not translatable fields).

    KSW_DEMO_LANG=ar python odoo-bin shell -c KSW_dev.conf < .../demo_lang_switch.py
    KSW_DEMO_LANG=en python odoo-bin shell -c KSW_dev.conf < .../demo_lang_switch.py

Reads the ticket ids staged by stage_helpdesk.py from _demo_ids.json. Dev only.
"""
import json
import os

LANG = os.environ.get("KSW_DEMO_LANG", "en")
IDS = json.load(open("custom_addons/KSW/docs/training/tools/_demo_ids.json"))

TEXT = {
    "ticket_new": {
        "en": ("Laptop won't turn on",
               "<p>The laptop does not power on at all since this morning. The "
               "charger LED is on, but pressing the power button does nothing - "
               "no fan, no screen. I tried a different power socket and holding "
               "the power button for 30 seconds.</p>"),
        "ar": ("الحاسب المحمول لا يعمل",
               "<p>الحاسب المحمول لا يعمل إطلاقًا منذ صباح اليوم. مؤشر الشاحن "
               "مضيء، لكن الضغط على زر التشغيل لا يُحدث أي استجابة — لا مروحة "
               "ولا شاشة. جرّبت مقبسًا كهربائيًا آخر، وضغطت زر التشغيل مطوّلًا "
               "لمدة ٣٠ ثانية.</p>"),
    },
    "ticket_in_progress": {
        "en": ("No internet in the 3rd floor meeting room",
               "<p>Wi-Fi shows connected but no pages load in the 3rd floor "
               "meeting room. Works normally at the desks outside. Started after "
               "yesterday's power cut.</p>"),
        "ar": ("لا يوجد اتصال بالإنترنت في قاعة اجتماعات الدور الثالث",
               "<p>تظهر شبكة الواي فاي متصلة، لكن لا تُفتح أي صفحة داخل قاعة "
               "اجتماعات الدور الثالث، بينما يعمل الاتصال بشكل طبيعي على المكاتب "
               "خارج القاعة. بدأت المشكلة بعد انقطاع التيار الكهربائي أمس.</p>"),
    },
    "ticket_blocked": {
        "en": ("Printer jams on double-sided printing",
               "<p>Every double-sided job jams in the rear tray. Single-sided "
               "printing works. Roughly 1 in 2 jobs.</p>"),
        "ar": ("انحشار الورق في الطابعة عند الطباعة على الوجهين",
               "<p>ينحشر الورق في الدرج الخلفي مع كل أمر طباعة على الوجهين، أما "
               "الطباعة على وجه واحد فتعمل بشكل سليم. تتكرر المشكلة في نحو نصف "
               "أوامر الطباعة.</p>"),
    },
    "ticket_overdue": {
        "en": ("Second monitor not detected on the docking station",
               "<p>The docking station only drives one of the two monitors since "
               "the last Windows update.</p>"),
        "ar": ("الشاشة الثانية غير معرَّفة على قاعدة التوصيل",
               "<p>قاعدة التوصيل تشغّل شاشة واحدة فقط من أصل شاشتين منذ آخر "
               "تحديث لنظام ويندوز.</p>"),
    },
    "ticket_request": {
        "en": ("Install AutoCAD on my workstation",
               "<p>I need AutoCAD 2024 for the new project drawings. The license "
               "was approved by my manager last week.</p>"),
        "ar": ("طلب تثبيت برنامج أوتوكاد على جهازي",
               "<p>أحتاج إلى برنامج AutoCAD 2024 لمخططات المشروع الجديد، وقد "
               "اعتمد مديري المباشر الرخصة الأسبوع الماضي.</p>"),
    },
    "ticket_request_access": {
        "en": ("Access to the shared Finance folder",
               "<p>Please grant read access to \\\\fileserver\\Finance\\Reports "
               "for the monthly closing.</p>"),
        "ar": ("طلب صلاحية الوصول إلى مجلد الإدارة المالية المشترك",
               "<p>أرجو منح صلاحية الاطّلاع على المجلد "
               "\\\\fileserver\\Finance\\Reports لأغراض الإقفال الشهري.</p>"),
    },
    "ticket_closed": {
        "en": ("Outlook not sending emails",
               "<p>Emails stay in the Outbox and are never sent. Receiving "
               "works.</p>"),
        "ar": ("برنامج أوتلوك لا يرسل البريد الإلكتروني",
               "<p>تبقى الرسائل في صندوق الصادر ولا تُرسل، أما الاستقبال فيعمل "
               "بشكل سليم.</p>"),
    },
    "ticket_closed_2": {
        "en": ("Password reset for the ERP account",
               "<p>Locked out after too many attempts. Please reset.</p>"),
        "ar": ("إعادة تعيين كلمة مرور حساب النظام",
               "<p>أُقفل الحساب بعد تكرار المحاولات الخاطئة، أرجو إعادة تعيين "
               "كلمة المرور.</p>"),
    },
    "ticket_closed_3": {
        "en": ("Replace faulty keyboard",
               "<p>Several keys stopped responding. Keyboard replaced from "
               "stock.</p>"),
        "ar": ("استبدال لوحة مفاتيح تالفة",
               "<p>توقفت عدة مفاتيح عن الاستجابة، وتم استبدال لوحة المفاتيح من "
               "المخزون.</p>"),
    },
    "ticket_closed_4": {
        "en": ("New employee laptop setup",
               "<p>Laptop, email account and VPN access for the new sales "
               "hire.</p>"),
        "ar": ("تجهيز حاسب محمول لموظف جديد",
               "<p>تجهيز حاسب محمول وحساب بريد إلكتروني وصلاحية الشبكة "
               "الافتراضية الخاصة لموظف المبيعات الجديد.</p>"),
    },
}

MAINT = {"en": "Keyboard not responding - sent to vendor",
         "ar": "لوحة المفاتيح لا تستجيب — أُرسل الجهاز إلى المورّد"}
ASSIGN_NOTE = {"en": "Handed over with charger and carry case.",
               "ar": "سُلّم الجهاز مع الشاحن وحقيبة الحمل."}
CHATTER = {
    "ticket_in_progress": {
        "en": "Checked the access point in the meeting room - it is up but not "
              "passing DHCP. Escalated to the network vendor.",
        "ar": "تم فحص نقطة الوصول في القاعة، وهي تعمل لكنها لا تمرّر عناوين "
              "DHCP. رُفع الأمر إلى مورّد الشبكة.",
    },
    "ticket_blocked": {
        "en": "Waiting for the replacement rear roller kit from the supplier "
              "(ETA 3 working days).",
        "ar": "بانتظار وصول طقم البكرات الخلفية البديل من المورّد (المدة "
              "المتوقعة ٣ أيام عمل).",
    },
}

count = 0
for key, texts in TEXT.items():
    tid = IDS.get(key)
    if not tid:
        continue
    ticket = env["helpdesk.ticket"].browse(tid).exists()
    if not ticket:
        continue
    name, desc = texts[LANG]
    ticket.write({"name": name, "description": desc})
    count += 1

# maintenance issue + assignment note
maint = env["it.asset.maintenance"].search(
    [("asset_id", "=", IDS.get("asset_maintenance", 0))], limit=1)
if maint:
    maint.issue = MAINT[LANG]
for assignment in env["it.asset.assignment"].search(
        [("asset_id", "in", [IDS.get("asset_laptop", 0), IDS.get("asset_phone", 0)])]):
    assignment.notes = ASSIGN_NOTE[LANG]

# chatter: replace the demo message bodies
for key, bodies in CHATTER.items():
    tid = IDS.get(key)
    if not tid:
        continue
    ticket = env["helpdesk.ticket"].browse(tid).exists()
    other = bodies["ar" if LANG == "en" else "en"]
    for msg in ticket.message_ids:
        if msg.body and other[:25] in msg.body:
            msg.body = "<p>%s</p>" % bodies[LANG]

env.cr.commit()
print("DEMO TEXT SWITCHED TO %s (%d tickets)" % (LANG.upper(), count))
