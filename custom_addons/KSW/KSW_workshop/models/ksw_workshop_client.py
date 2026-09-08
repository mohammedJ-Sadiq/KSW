from odoo import _, api, fields, models


class KswWorkshopClient(models.Model):
    """A partner's *workshop client* role.

    Not a second party table and not a boolean on res.partner: a role
    assignment row, the same shape SAP expresses as a BP role and Oracle TCA
    as a party usage. The party master stays `res.partner` — see the standing
    rule in the vault (Odoo 19 Pitfalls #43).

    Why it exists: `customer_rank > 0` alone stopped being a useful filter the
    moment the BAS customer sync landed. 819 partners carry an accounting
    customer role on odoo_dev and one of them owns any vehicle, so the Client
    picker on a workshop request had to be narrowed to the clients the
    workshop actually serves. Registration is that narrowing, maintained by
    the workshop manager under Workshop -> Configuration -> Clients.
    """
    _name = 'ksw.workshop.client'
    _description = 'Workshop Client Registration'
    _order = 'partner_id'

    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, ondelete='cascade',
        domain="[('customer_rank', '>', 0)]",
        help="The contact carrying the customer role. Registering it here is what "
             "makes it selectable on a workshop request and on a fleet vehicle.")
    active = fields.Boolean(default=True)
    note = fields.Char()

    _partner_uniq = models.Constraint(
        'unique(partner_id)',
        'This client is already registered with the workshop.',
    )

    @api.depends('partner_id')
    def _compute_display_name(self):
        # _compute_display_name, never name_get — the ORM stopped calling
        # name_get in Odoo 19 and an override there is silently ignored.
        for registration in self:
            registration.display_name = registration.partner_id.display_name or _('New')

    @api.model_create_multi
    def create(self, vals_list):
        """Revive an archived registration instead of hitting the unique index.

        Unregistering is an archive, so without this "unregister, then change
        your mind" is a dead end that surfaces as a raw Postgres unique
        violation rather than anything a manager can act on.
        """
        revived = self.browse()
        remaining = []
        for vals in vals_list:
            partner_id = vals.get('partner_id')
            existing = self.with_context(active_test=False).search(
                [('partner_id', '=', partner_id)], limit=1,
            ) if partner_id else self.browse()
            if existing:
                existing.write(dict(vals, active=True))
                revived |= existing
            else:
                remaining.append(vals)
        return revived | super().create(remaining)


def _register_existing_clients(env):
    """Register every partner the workshop already points at.

    Called from both the post-install hook and the 19.0.8.0.0 migration, so
    a fresh install and an upgrade cannot drift apart — the same arrangement
    KSW_fleet uses for its customer_rank stamping.

    Without this the registry starts empty, and every one of the 17,080
    imported requests plus 371 vehicles would point at a client outside its
    own field's domain — including `client_id`'s own default,
    `env.company.partner_id`.

    Idempotent: only partners with no registration row (archived rows count)
    are added.
    """
    Registration = env['ksw.workshop.client'].sudo()

    partner_ids = set()
    # search([]) rather than env.company: odoo_dev carries two companies and
    # both their partners are legitimate workshop clients.
    partner_ids.update(env['res.company'].sudo().search([]).mapped('partner_id').ids)
    env.cr.execute("""
        SELECT DISTINCT client_id FROM ksw_workshop_request WHERE client_id IS NOT NULL
        UNION
        SELECT DISTINCT client_id FROM ksw_fleet_vehicle WHERE client_id IS NOT NULL
    """)
    partner_ids.update(row[0] for row in env.cr.fetchall())

    known = set(Registration.with_context(active_test=False).search([
        ('partner_id', 'in', list(partner_ids)),
    ]).mapped('partner_id').ids)
    missing = partner_ids - known
    if missing:
        Registration.create([{'partner_id': pid} for pid in sorted(missing)])
    return len(missing)
