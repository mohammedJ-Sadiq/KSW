"""The workshop request's Client picker offers customers only.

The workshop-side half of the rule pinned down in
``KSW_fleet/tests/test_client_is_partner_role.py``: a client is a
``res.partner`` carrying the customer role (``customer_rank > 0``), not an
arbitrary contact, and not a Workshop-specific flag.

The bug these guard: the picker used to offer every partner in the database,
including all 534 employee contacts — a client is a reserved entity and an
employee is not one.
"""
import ast

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopClientIsPartnerRole(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']

        cls.client_partner = cls.Partner.create({
            'name': 'WS Role Test Client', 'customer_rank': 1,
        })
        cls.employee_partner = cls.Partner.create({
            'name': 'WS Role Test Employee Contact',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'WS Role Test Employee',
            'work_contact_id': cls.employee_partner.id,
        })

    def _client_domain(self):
        return self.env['ksw.workshop.request'].fields_get(
            ['client_id'])['client_id']['domain']

    def _picker_results(self):
        return self.Partner.search(ast.literal_eval(self._client_domain()))

    def test_client_id_carries_the_customer_rank_domain(self):
        self.assertIn('customer_rank', self._client_domain())

    def test_workshop_and_fleet_pickers_agree(self):
        """vehicle_id's domain joins on client_id, so the two must match.

        If they diverged, a client pickable on the request could own no
        vehicle pickable on the same form.
        """
        fleet_domain = self.env['ksw.fleet.vehicle'].fields_get(
            ['client_id'])['client_id']['domain']
        self.assertEqual(
            ast.literal_eval(self._client_domain()),
            ast.literal_eval(fleet_domain),
        )

    def test_picker_includes_a_customer(self):
        self.assertIn(self.client_partner, self._picker_results())

    def test_picker_excludes_an_employee_contact(self):
        self.assertTrue(
            self.employee_partner.employee,
            "fixture precondition: hr should have flagged this contact",
        )
        self.assertNotIn(self.employee_partner, self._picker_results())

    def test_no_employee_contact_is_offered_as_a_client(self):
        offered = self._picker_results()
        self.assertFalse(
            offered.filtered(lambda p: p.employee and not p.customer_rank),
            "an employee contact with no customer role reached the picker",
        )

    def test_company_partner_is_still_a_valid_client(self):
        """All 17,080 imported requests point at it, and it is the default."""
        self.assertIn(self.env.company.partner_id, self._picker_results())

    def test_default_client_is_inside_its_own_domain(self):
        default = self.env['ksw.workshop.request'].default_get(
            ['client_id'])['client_id']
        self.assertIn(
            self.Partner.browse(default), self._picker_results(),
            "the field's default value falls outside its own domain",
        )

    def test_existing_request_clients_all_satisfy_the_domain(self):
        offending = self.env['ksw.workshop.request'].search([
            ('client_id', '!=', False),
            ('client_id.customer_rank', '<=', 0),
        ])
        self.assertFalse(
            offending,
            f"{len(offending)} request(s) point at a client outside the new "
            f"domain: {offending[:5].mapped('client_id.name')}",
        )
