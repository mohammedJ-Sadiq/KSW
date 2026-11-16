{
    'name': 'KSW Documents',
    'version': '19.0.1.3.0',
    'summary': 'Vehicle and employee documents with expiry: istimara, insurance, inspection, licences, iqama; read from an upload by AI',
    'description': """
Documents Register
==================
The documents of each company vehicle (registration / istimara, insurance,
periodic inspection, operation card) and each employee (driving licence,
driver card, iqama, passport, visa), with number, dates and the scan. Which
documents are required is configuration; each one is valid, expiring soon,
expired or missing, and HR is told 30 days before an expiry.

Upload a photo or PDF and Claude reads the type, number and dates (Hijri
converted to Gregorian) into a draft that HR confirms: nothing is trusted
without a person. The register is the single place these are entered: an
employee's iqama, passport and visa are written back to Odoo's own fields.
    """,
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': ['hr', 'mail', 'KSW_fleet', 'KSW_ai_assistant'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/document_types.xml',
        'data/cron.xml',
        'wizard/upload_wizard_views.xml',
        'views/ksw_document_views.xml',
        'views/owner_views.xml',
        'views/batch_views.xml',
        'views/registration_views.xml',
        'views/import_views.xml',
        'views/menus.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
