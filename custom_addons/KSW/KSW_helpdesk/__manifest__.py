{
    'name': 'KSW Helpdesk',
    'version': '19.0.1.3.0',
    'summary': 'Internal IT support ticketing system and asset register for employees',
    'description': """
Helpdesk / IT Ticketing for KSW
================================

Lets any employee raise a support ticket and lets the IT team triage,
assign and resolve it:

- Incident / Service Request split, each with its own categories and
  ticket numbering (INC..... / REQ.....)
- Caller card on every ticket (email, phone, job, department)
- Employee self-service: submit a ticket, track its status, comment,
  rate the resolution; "Reported By" is limited to yourself, or your
  direct reports if you are a manager
- Kanban board grouped by stage with priority stars, blocked/ready state
  and overdue badges
- Automatic notification to the assigned agent
- Automatic email to the requester when their ticket is closed
- Reporting (pivot/graph) and a calendar of ticket deadlines

Just two roles: every employee submits tickets (for themselves, or a
direct report); the IT Team handles everything else.

IT Asset Register (IT Team only)
---------------------------------
- Asset register with category, brand, model, serial number, purchase
  and warranty information
- Assign / return workflow with a full assignment history per employee
- Maintenance tracking (send to / return from repair)
- Optional link from a ticket to the specific IT asset it's about
- Automatic activity reminder 30 days before an asset's warranty expires

Other employees never see the asset register itself - only the IT Team
does. The optional "Related Asset" field on a ticket only ever shows
assets already assigned to that employee (or their direct report).
""",
    'author': 'KSW',
    'category': 'Services/Helpdesk',
    'depends': [
        'hr',
        'mail',
        # supplier_rank — the vendor-role marker used by the vendor pickers'
        # domains — is defined in account (addons/account/models/partner.py).
        'account',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/helpdesk_ticket_stage_data.xml',
        'data/helpdesk_ticket_category_data.xml',
        'data/it_asset_category_data.xml',
        'data/mail_template_ticket_closed.xml',
        'data/ir_cron_data.xml',
        'views/helpdesk_ticket_stage_views.xml',
        'views/helpdesk_ticket_category_views.xml',
        'views/helpdesk_ticket_views.xml',
        'views/it_asset_category_views.xml',
        'views/it_asset_assignment_views.xml',
        'views/it_asset_maintenance_views.xml',
        'views/it_asset_views.xml',
        'wizard/it_asset_assign_wizard_views.xml',
        'wizard/it_asset_return_wizard_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
