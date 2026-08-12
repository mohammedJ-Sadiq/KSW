{
    'name': 'KSW Commissions & Other Allowances',
    'version': '19.0.3.2.0',
    'summary': 'Commissions and allowances on the ERP element model: a '
               'configurable pay-component catalog, one entry screen per '
               'department, and a monthly run the General Manager approves.',
    'description': """
Extra pay — overtime, driver trips, meals, allowances, bonuses — recorded
the way the large HR systems do it: the *type* is configuration data (SAP's
wage type, Oracle's element), not code, so adding a new kind of pay is a
catalog record rather than a model.

  ksw.pay.component   the catalog: fixed | qty x rate | wage-derived | tiered
  ksw.pay.entry       the only fact — one occurrence for one employee
  ksw.pay.batch       one component, one scope, one month: the entry screen
  ksw.pay.submission  one department's handover to the General Manager
  ksw.pay.run         the month, its approval and the period lock
  ksw.pay.run.line    the payment register, generated, feeding the bank file

Each supervisor records and submits only his own department; the General
Manager sees which departments are in, who is due to be paid and what their
parked loan installments consume, and approves the month in one action.
Approval settles those installments against KSW_deduction and locks the
period.

Sales & collection commission is computed here but paid separately — it is
not part of the commission request.
""",
    'author': 'KSW',
    'category': 'Human Resources',
    'depends': [
        'hr',
        'mail',
        'KSW_working_schedule',
        'KSW_attendance_sheet',
        'KSW_deduction',
        'KSW_ext_sync',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'data/pay_component_data.xml',
        'data/mail_template_data.xml',
        'views/ksw_site_views.xml',
        'views/ksw_pay_component_views.xml',
        'views/ksw_pay_batch_views.xml',
        'views/ksw_pay_submission_views.xml',
        'views/ksw_pay_run_views.xml',
        'views/ksw_salesperson_profile_views.xml',
        'views/ksw_sales_commission_rule_views.xml',
        'views/ksw_sales_commission_sheet_views.xml',
        'wizard/sales_commission_override_wizard_views.xml',
        'wizard/sales_commission_import_wizard_views.xml',
        'views/ksw_commission_bank_export_wizard_views.xml',
        'views/hr_employee_views.xml',
        'views/res_partner_views.xml',
        'views/ksw_deduction_views.xml',
        'views/res_config_settings_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}

