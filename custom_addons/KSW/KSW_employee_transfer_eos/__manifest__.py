{
    'name': 'KSW Employee Transfer: End of Service',
    'version': '19.0.1.1.0',
    'summary': 'A GM end-of-service decision opens the End of Service request in Time Off',
    'description': """
When the GM ends the service of a released employee, his decision opens the
End of Service request in Time Off (termination date = the decision's date,
Article 84). Its approval chain and EOS payslip run there; nothing waits for
HR in Employee Transfers. The released employee is done when that request is
validated; refused or cancelled there, he is back on the GM's list.

Installs by itself when Employee Transfer and End of Service are both present.
    """,

    'author': 'KSW',
    'category': 'Human Resources',
    'depends': ['KSW_employee_transfer', 'KSW_eos_leave'],
    'data': ['views/release_line_views.xml'],
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
}
