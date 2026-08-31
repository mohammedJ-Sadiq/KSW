"""The IT asset vendor pickers offer vendors only.

The supplier-side twin of the client rule (see
``KSW_fleet/tests/test_client_is_partner_role.py``): one party master, many
roles. ``supplier_rank > 0`` is Odoo's native vendor marker, the same one
core's purchase / product_supplierinfo pickers use.

Before this, ``it.asset.supplier_id`` and ``it.asset.maintenance.vendor_id``
carried no domain, so both offered every contact in the database — employees
included.
"""
import ast

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVendorIsPartnerRole(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']

        cls.vendor = cls.Partner.create({
            'name': 'IT Role Test Vendor', 'supplier_rank': 1,
        })
        cls.employee_partner = cls.Partner.create({
            'name': 'IT Role Test Employee Contact',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'IT Role Test Employee',
            'work_contact_id': cls.employee_partner.id,
        })

    def _domain(self, model, field):
        return self.env[model].fields_get([field])[field]['domain']

    def _picker_results(self, model, field):
        return self.Partner.search(ast.literal_eval(self._domain(model, field)))

    # it.asset.supplier_id ---------------------------------------------
    def test_asset_supplier_carries_the_supplier_rank_domain(self):
        self.assertIn('supplier_rank', self._domain('it.asset', 'supplier_id'))

    def test_asset_supplier_picker_includes_a_vendor(self):
        self.assertIn(
            self.vendor, self._picker_results('it.asset', 'supplier_id'))

    def test_asset_supplier_picker_excludes_an_employee_contact(self):
        self.assertTrue(self.employee_partner.employee)
        self.assertNotIn(
            self.employee_partner,
            self._picker_results('it.asset', 'supplier_id'),
        )

    # it.asset.maintenance.vendor_id -----------------------------------
    def test_maintenance_vendor_carries_the_supplier_rank_domain(self):
        self.assertIn(
            'supplier_rank',
            self._domain('it.asset.maintenance', 'vendor_id'),
        )

    def test_maintenance_vendor_picker_excludes_an_employee_contact(self):
        self.assertNotIn(
            self.employee_partner,
            self._picker_results('it.asset.maintenance', 'vendor_id'),
        )

    # existing data -----------------------------------------------------
    def test_existing_vendor_references_satisfy_the_domain(self):
        assets = self.env['it.asset'].search([
            ('supplier_id', '!=', False),
            ('supplier_id.supplier_rank', '<=', 0),
        ])
        self.assertFalse(
            assets,
            "existing asset(s) point at a vendor outside the new domain: "
            f"{assets.mapped('supplier_id.name')[:5]}",
        )
        maints = self.env['it.asset.maintenance'].search([
            ('vendor_id', '!=', False),
            ('vendor_id.supplier_rank', '<=', 0),
        ])
        self.assertFalse(
            maints,
            "existing maintenance record(s) point at a vendor outside the "
            f"new domain: {maints.mapped('vendor_id.name')[:5]}",
        )
