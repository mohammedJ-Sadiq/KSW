"""One item line of a BAS delivery note, as «الحركة التجارية للأصناف» lists it.

A snapshot taken by the selection screen (``ksw.bas.item.movement.wizard``)
for one run, never a mirror: BAS is read live every time Show is pressed, so
the list is exactly what BAS holds at that moment, and the rows go away with
the run. Nothing else in Odoo reads them.

The two multipliers are kept apart on purpose:

* **«الرد المضاعف»** = ``cod10.FACTORE`` — the customer's multiplier *as it is
  now* in the BAS customer master. Live: changing a customer's ratio changes
  this column for every past line of that customer.
* **«رد الفاتورة»** = ``vou10.PHRDIS`` — the multiplier recorded on the
  invoice when it was issued. This is what driver commissions are paid on
  from 6 Sep 2026 (see KSW_commissions ``ksw_pay_import_bas``).

Where they differ, the customer's ratio was changed after the invoice.
"""
from odoo import fields, models


class KswBasItemMovementLine(models.TransientModel):
    _name = 'ksw.bas.item.movement.line'
    _description = 'BAS Item Movement Line'
    _order = 'date desc, document, id'
    # A report can stay open on someone's screen for a working day.
    _transient_max_hours = 12.0

    wizard_id = fields.Many2one(
        'ksw.bas.item.movement.wizard', required=True, ondelete='cascade', index=True)

    date = fields.Date(string='Date', readonly=True)
    document = fields.Char(string='Document', readonly=True,
                           help='BAS branch / document number (CODE2 / NUMBER1).')
    bas_key = fields.Char(string='BAS Key', readonly=True,
                          help='FTYPE/FTYPE2/CODE2/NUMBER1 of the delivery note.')
    branch_id = fields.Many2one('ksw.bas.branch', string='Branch', readonly=True)
    warehouse = fields.Char(string='Warehouse', readonly=True,
                            help='BAS COST_CENTER3 of the delivery note.')

    customer_code = fields.Char(string='Customer Account', readonly=True)
    customer_name = fields.Char(string='Customer', readonly=True)

    item_code = fields.Char(string='Item Code', readonly=True)
    item_name = fields.Char(string='Item', readonly=True)
    quantity = fields.Float(string='Quantity', readonly=True, aggregator='sum')
    unit = fields.Char(string='Unit', readonly=True)
    price = fields.Float(string='Unit Price', readonly=True, aggregator=False)
    amount = fields.Float(string='Amount', readonly=True, aggregator='sum')
    tax_amount = fields.Float(string='VAT', readonly=True, aggregator='sum')
    total = fields.Float(string='Total incl. VAT', readonly=True, aggregator='sum')

    vehicle_account_id = fields.Many2one(
        'account.analytic.account', string='Vehicle Cost Centre', readonly=True,
        help='BAS COST_CENTER (the vehicle or department), as an Odoo analytic account.')
    vehicle_cost_centre = fields.Char(
        string='Vehicle Cost Centre (BAS)', readonly=True,
        help='The name exactly as BAS holds it, kept even when no analytic account matches.')
    driver_account_id = fields.Many2one(
        'account.analytic.account', string='Driver Cost Centre', readonly=True,
        help='BAS COST_CENTER2 («مركز تكلفة الموظف»), as an Odoo analytic account.')
    driver_cost_centre = fields.Char(
        string='Driver Cost Centre (BAS)', readonly=True,
        help='The name exactly as BAS holds it, kept even when no analytic account matches.')

    customer_factor = fields.Float(
        string='Customer Multiplier', readonly=True, aggregator='sum', digits=(16, 2),
        help='«الرد المضاعف»: the customer\'s distance multiplier as it stands now '
             'in the BAS customer master (cod10.FACTORE).')
    invoice_factor = fields.Float(
        string='Invoice Multiplier', readonly=True, aggregator='sum', digits=(16, 2),
        help='«رد الفاتورة»: the multiplier recorded on the invoice when it was '
             'issued (vou10.PHRDIS).')
    factor_difference = fields.Float(
        string='Difference', readonly=True, aggregator='sum', digits=(16, 2),
        help='Invoice multiplier minus customer multiplier.')
    factors_differ = fields.Boolean(string='Multipliers Differ', readonly=True)
    invoice_factor_missing = fields.Boolean(
        string='No Invoice Multiplier', readonly=True,
        help='The invoice carries no «رد الفاتورة».')
