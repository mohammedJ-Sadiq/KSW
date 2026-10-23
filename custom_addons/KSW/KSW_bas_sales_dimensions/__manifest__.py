{
    'name': 'KSW BAS Sales Dimensions',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'category': 'Accounting',
    'summary': 'Vehicle, driver and salesperson on BAS sales invoices and journal items',
    'description': """
BAS books a month's credit sales as one revenue line per branch on the 002
invoice; the vehicle (COST_CENTER) and driver (COST_CENTER2) are only on each
delivery note (600), and the salesperson (SELLER) on the document. The import
already rebuilds each invoice into one line per delivery note. This bridge:

* tags every such line with its note's vehicle (Cost Centres plan) and driver
  (Employee Cost Centre plan), as BAS tags the note itself;
* sets the invoice's Salesperson from BAS's seller through BAS Sales Reps
  (house accounts and branch placeholders stay empty, never OdooBot);
* shows Salesperson as a column on journal items.

A bridge because it needs both KSW_accounting_ux (the BAS ledger) and
KSW_commissions (the sales-rep mapping); installs itself when both are present.
""",
    'depends': ['KSW_accounting_ux', 'KSW_commissions'],
    'data': ['views/account_move_line_views.xml'],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
