"""Only registered clients are offered on a workshop request.

The customer role (``customer_rank > 0``) was the whole filter until the BAS
customer sync landed: 819 partners then carried it and exactly one of them
owned a vehicle. The picker is now narrowed a second time, to the clients the
workshop manager registered under Configuration -> Clients.

These assert **the domain and what it selects**, never a write expected to
fail — a Many2one ``domain=`` is a UI hint in Odoo, not an ORM constraint, and
a test that expects a write to raise would be testing something the framework
does not promise.
"""
import ast

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopClientRegistry(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']
        cls.Registration = cls.env['ksw.workshop.client']

        cls.registered = cls.Partner.create({
            'name': 'WS Registry Registered Client', 'customer_rank': 1,
        })
        cls.registration = cls.Registration.create({'partner_id': cls.registered.id})

        # A perfectly good accounting customer that the workshop does not
        # serve — the 818 of them are the reason this feature exists.
        cls.unregistered = cls.Partner.create({
            'name': 'WS Registry Accounting-Only Customer', 'customer_rank': 1,
        })

    def _client_domain(self, model='ksw.workshop.request'):
        return self.env[model].fields_get(['client_id'])['client_id']['domain']

    def _picker_results(self):
        return self.Partner.search(ast.literal_eval(self._client_domain()))

    # ------------------------------------------------------------------
    # The domain
    # ------------------------------------------------------------------
    def test_picker_offers_a_registered_client(self):
        self.assertIn(self.registered, self._picker_results())

    def test_picker_excludes_a_customer_nobody_registered(self):
        self.assertNotIn(self.unregistered, self._picker_results())

    def test_request_and_vehicle_pickers_stay_identical(self):
        """vehicle_id's domain joins on client_id, so the two must agree.

        If they diverged, a client pickable on the request could own no
        vehicle pickable on the same form.
        """
        self.assertEqual(
            ast.literal_eval(self._client_domain()),
            ast.literal_eval(self._client_domain('ksw.fleet.vehicle')),
        )

    def test_domain_tests_the_registration_is_active(self):
        """Not [('x_workshop_client_ids', '!=', False)].

        That form tests the relation directly and never applies the comodel's
        active filter, so an archived registration keeps its client in every
        picker — silently, since the domain still reads correctly.
        """
        for model in ('ksw.workshop.request', 'ksw.fleet.vehicle'):
            self.assertIn(
                ('x_workshop_client_ids.active', '=', True),
                ast.literal_eval(self._client_domain(model)),
                f'{model}.client_id must test the registration, not just the relation',
            )

    def test_client_id_default_is_inside_its_own_domain(self):
        default = self.env['ksw.workshop.request'].default_get(['client_id'])['client_id']
        self.assertIn(
            self.Partner.browse(default), self._picker_results(),
            "the field's default value falls outside its own domain",
        )

    # ------------------------------------------------------------------
    # Archiving is how a client is retired
    # ------------------------------------------------------------------
    def test_archiving_a_registration_removes_the_client_from_the_picker(self):
        self.registration.action_archive()
        self.assertNotIn(self.registered, self._picker_results())

    def test_re_registering_an_archived_client_revives_it(self):
        """Otherwise "unregister, then change your mind" is a unique violation."""
        self.registration.action_archive()
        revived = self.Registration.create({'partner_id': self.registered.id})
        self.assertEqual(revived, self.registration)
        self.assertTrue(revived.active)
        self.assertIn(self.registered, self._picker_results())
        self.assertEqual(
            self.Registration.with_context(active_test=False).search_count(
                [('partner_id', '=', self.registered.id)]),
            1,
            'reviving must not leave a second registration behind',
        )

    # ------------------------------------------------------------------
    # Nothing that already exists became unreachable
    # ------------------------------------------------------------------
    def test_every_existing_request_client_is_registered(self):
        offending = self.env['ksw.workshop.request'].search([
            ('client_id', '!=', False),
            ('client_id.x_workshop_client_ids', '=', False),
        ])
        self.assertFalse(
            offending,
            f'{len(offending)} request(s) point at an unregistered client: '
            f'{offending[:5].mapped("client_id.name")}',
        )

    def test_every_existing_vehicle_client_is_registered(self):
        offending = self.env['ksw.fleet.vehicle'].search([
            ('client_id.x_workshop_client_ids', '=', False),
        ])
        self.assertFalse(
            offending,
            f'{len(offending)} vehicle(s) belong to an unregistered client: '
            f'{offending[:5].mapped("client_id.name")}',
        )

    def test_every_company_partner_is_registered(self):
        """client_id defaults to env.company.partner_id, and odoo_dev has two companies."""
        for company in self.env['res.company'].search([]):
            self.assertTrue(
                company.partner_id.x_workshop_client_ids,
                f'{company.name} is not a registered workshop client',
            )

    # ------------------------------------------------------------------
    # Reach
    # ------------------------------------------------------------------
    def test_a_plain_employee_can_resolve_the_picker(self):
        """The domain traverses x_workshop_client_ids, so it runs the
        registry's own _search and enforces its ACL. Without a read row for
        base.group_user, every employee's Client picker raises AccessError.
        """
        user = self.env['res.users'].create({
            'name': 'WS Registry Plain', 'login': 'ws_registry_plain',
            'email': 'ws_registry_plain@wsreg.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        offered = self.Partner.with_user(user).search(
            ast.literal_eval(self._client_domain()))
        self.assertIn(self.registered, offered)
        self.assertNotIn(self.unregistered, offered)
