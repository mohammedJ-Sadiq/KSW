"""A client is a res.partner carrying the customer role.

One party master, many roles — the rule SAP (BP roles), Oracle Fusion (TCA
party usages), Dynamics (DirPartyTable) and Odoo all share. Odoo expresses
the role as ``customer_rank`` / ``supplier_rank``; it deleted the old
``customer``/``supplier`` booleans in v13 precisely so apps would stop
owning their own marker.

These tests pin that down for ``ksw.fleet.vehicle.client_id``: the picker
offers customers only, never the ~534 employee contacts, and the company's
own partner — the field's default, and the client on every vehicle imported
from history — stays inside the domain.

A Many2one ``domain=`` is a client-side hint, not an ORM constraint (core's
sale/repair/pos add no constraint either), so these assert the *domain* and
what it selects, not a write that fails.
"""
import ast

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestClientIsPartnerRole(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']

        cls.client_partner = cls.Partner.create({
            'name': 'Role Test Client', 'customer_rank': 1,
        })
        cls.plain_partner = cls.Partner.create({'name': 'Role Test Plain'})
        cls.employee_partner = cls.Partner.create({
            'name': 'Role Test Employee Contact',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Role Test Employee',
            'work_contact_id': cls.employee_partner.id,
        })

    def _client_domain(self, model='ksw.fleet.vehicle', field='client_id'):
        """The domain the web client actually receives for the picker."""
        return self.env[model].fields_get([field])[field]['domain']

    def _picker_results(self, model='ksw.fleet.vehicle', field='client_id'):
        return self.Partner.search(
            ast.literal_eval(self._client_domain(model, field)))

    # ------------------------------------------------------------------
    # The domain reaches the client
    # ------------------------------------------------------------------
    def test_client_id_carries_the_customer_rank_domain(self):
        self.assertIn('customer_rank', self._client_domain())

    # ------------------------------------------------------------------
    # What it selects
    # ------------------------------------------------------------------
    def test_picker_includes_a_customer(self):
        self.assertIn(self.client_partner, self._picker_results())

    def test_picker_excludes_an_employee_contact(self):
        """The reported bug: employees offered as clients."""
        self.assertTrue(
            self.employee_partner.employee,
            "fixture precondition: hr should have flagged this contact",
        )
        self.assertNotIn(self.employee_partner, self._picker_results())

    def test_picker_excludes_an_unranked_contact(self):
        self.assertNotIn(self.plain_partner, self._picker_results())

    def test_no_employee_contact_is_offered_as_a_client(self):
        """Asserts the negative across the whole database, not one fixture."""
        offered = self._picker_results()
        self.assertFalse(
            offered.filtered(lambda p: p.employee and not p.customer_rank),
            "an employee contact with no customer role reached the picker",
        )

    # ------------------------------------------------------------------
    # The company's own fleet is still a client
    # ------------------------------------------------------------------
    def test_company_partner_satisfies_the_domain(self):
        """Guards the post_init_hook and the 19.0.2.1.0 migration.

        client_id defaults to env.company.partner_id, so if the stamp were
        missing the default would fall outside its own domain on every new
        record.
        """
        self.assertIn(self.env.company.partner_id, self._picker_results())

    def test_every_company_partner_is_stamped(self):
        unstamped = self.env['res.company'].search([]).partner_id.filtered(
            lambda p: not p.customer_rank)
        self.assertFalse(
            unstamped, f"company partner(s) missing the customer role: "
                       f"{unstamped.mapped('name')}")

    # ------------------------------------------------------------------
    # Existing data stays reachable
    # ------------------------------------------------------------------
    def test_existing_vehicle_clients_all_satisfy_the_domain(self):
        offending = self.env['ksw.fleet.vehicle'].search([
            ('client_id.customer_rank', '<=', 0),
        ])
        self.assertFalse(
            offending,
            f"{len(offending)} vehicle(s) point at a client outside the new "
            f"domain: {offending[:5].mapped('client_id.name')}",
        )
