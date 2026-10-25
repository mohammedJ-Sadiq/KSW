from datetime import datetime

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasVoucherLines(TransactionCase):
    """A BAS header slot left at AMOUNT 0 never becomes a 0/0 journal line."""

    def test_zero_header_slots_are_skipped(self):
        Account = self.env['account.account']
        client = Account.create({'code': '129997001', 'name': 'KSW zero client',
                                 'account_type': 'asset_receivable', 'reconcile': True})
        default_sales = Account.create({'code': '4101997002', 'name': 'KSW default sales',
                                        'account_type': 'income'})
        item_sales = Account.create({'code': '4101997003', 'name': 'KSW item sales',
                                     'account_type': 'income'})
        vat = Account.create({'code': '2119997004', 'name': 'KSW VAT',
                              'account_type': 'liability_current'})
        accounts = {'120301281': client.id, '4101120002': default_sales.id,
                    '4101040002': item_sales.id, '2119010001': vat.id}

        def row(serial, f, t, amount):
            # TAX_AMOUNT is the document's VAT, repeated on every row (as in BAS)
            return {'SERIAL': serial, 'FCODE': f, 'TCODE': t, 'AMOUNT': amount,
                    'BAMOUNT': 552.0, 'TAX_AMOUNT': 72.0,
                    'FDATE': datetime(2026, 10, 20, 8, 43)}

        # BAS voucher 002/3 br110 #22573, rows as stored in vou10
        lines = [row(1, '120301281', '', 0.0), row(2, '', '4101120002', 0.0),
                 row(3, '', '4101040002', 480.0), row(4, '', '2119010001', 72.0),
                 row(5, '120301281', '', 552.0)]
        problems = []
        vals = self.env['ksw.bas.gl.import']._voucher_to_move(
            '002/3/110/22573', '002', 3, '110', 22573.0, lines, accounts,
            vat.id, self.env.company, problems)

        self.assertFalse(problems)
        items = [cmd[2] for cmd in vals['line_ids']]
        self.assertEqual(
            sorted((i['account_id'], i['debit'], i['credit']) for i in items),
            sorted([(item_sales.id, 0.0, 480.0), (vat.id, 0.0, 72.0),
                    (client.id, 552.0, 0.0)]))
        self.assertNotIn(default_sales.id, [i['account_id'] for i in items])
