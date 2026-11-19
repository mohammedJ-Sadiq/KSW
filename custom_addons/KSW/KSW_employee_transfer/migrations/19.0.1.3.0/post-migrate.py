# Tracking keeps the status label in the clicking user's language.
LABELS = {
    'draft': ('Draft', 'مسودة'),
    'hr': ('HR Review', 'مراجعة الموارد البشرية'),
    'gm': ('GM Approval', 'اعتماد المدير العام'),
    'approved': ('Approved, Waiting for Date', 'معتمد، بانتظار تاريخ النفاذ'),
    'applied': ('Applied', 'منفّذ'),
    'refused': ('Refused', 'مرفوض'),
    'cancelled': ('Cancelled', 'ملغي'),
}


def _key(column):
    whens = ' '.join("WHEN %s IN ('%s', '%s') THEN '%s'" % (column, en, ar, key)
                     for key, (en, ar) in LABELS.items())
    return 'CASE %s END' % whens


def migrate(cr, version):
    """Step stamps for transfers made before they existed, read back from the
    chatter's status tracking (who moved it to which status, and when). The
    previous manager / approvers were never recorded: they stay empty."""
    cr.execute("""
        WITH moves AS (
            SELECT m.res_id, m.date, """ + _key('v.old_value_char') + """ AS old,
                   """ + _key('v.new_value_char') + """ AS new,
                   (SELECT u.id FROM res_users u WHERE u.partner_id = m.author_id ORDER BY u.id LIMIT 1) AS uid
              FROM mail_message m
              JOIN mail_tracking_value v ON v.mail_message_id = m.id
              JOIN ir_model_fields f ON f.id = v.field_id
             WHERE m.model = 'ksw.employee.transfer' AND f.model = 'ksw.employee.transfer' AND f.name = 'state'
        )
        UPDATE ksw_employee_transfer t SET
            submitted_date = COALESCE(t.submitted_date,
                (SELECT max(date) FROM moves WHERE res_id = t.id AND new = 'hr')),
            hr_user_id = COALESCE(t.hr_user_id,
                (SELECT uid FROM moves WHERE res_id = t.id AND old = 'hr' AND new = 'gm'
                  ORDER BY date DESC LIMIT 1)),
            hr_date = COALESCE(t.hr_date,
                (SELECT max(date) FROM moves WHERE res_id = t.id AND old = 'hr' AND new = 'gm')),
            gm_user_id = COALESCE(t.gm_user_id,
                (SELECT uid FROM moves WHERE res_id = t.id AND old = 'gm'
                  AND new IN ('approved', 'applied') ORDER BY date DESC LIMIT 1)),
            gm_date = COALESCE(t.gm_date,
                (SELECT max(date) FROM moves WHERE res_id = t.id AND old = 'gm'
                  AND new IN ('approved', 'applied'))),
            closed_by_id = COALESCE(t.closed_by_id,
                (SELECT uid FROM moves WHERE res_id = t.id AND new IN ('refused', 'cancelled')
                  ORDER BY date DESC LIMIT 1)),
            closed_date = COALESCE(t.closed_date,
                (SELECT max(date) FROM moves WHERE res_id = t.id AND new IN ('refused', 'cancelled')))
    """)
