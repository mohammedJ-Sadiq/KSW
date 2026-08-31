"""19.0.7.0.0 — archive the legacy-import placeholder employees.

scripts/import_history.py creates a bare hr.employee for every legacy
requester who had no Odoo account, because ksw.workshop.request.employee_id
is required and 7,256 of the 17,079 imported rows were submitted by people
who never had one. They are pure FK anchors: the submitted name and email
are already stored on the request itself (x_legacy_requester_name /
x_legacy_requester_email), and the form shows them in the "Legacy Import"
group, so nothing is lost by hiding the placeholder.

Left active, though, they were 83 extra rows in Employees — inflating the
headcount and, worse, appearing in every hr.employee picker as near-duplicate
misspellings of real names ("Anwar ull haq" has twelve spellings). Archiving
removes them from the default list, from the count, and from name_search
(pickers), while keeping every request's requester link intact and still
reachable under the Archived filter.

Not a delete: the FK is real and 7,256 rows depend on it.

ORM rather than raw SQL here (unlike the 19.0.6.0.0 migration next door):
hr.employee.active is a stored related on resource_resource.active, so a raw
UPDATE would desynchronise the resource. `active` carries no tracking=True,
and no_wizard/batch keeps action_archive from opening the departure wizard,
so there is no chatter or notification storm — the reason SQL was needed for
the 17k-row request update does not apply to 83 employees.

Idempotent: only still-active placeholders are touched.
"""

from odoo import SUPERUSER_ID, api

from odoo.addons.KSW_workshop.models.ksw_workshop_request import (
    LEGACY_PLACEHOLDER_SUFFIX,
)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    placeholders = env['hr.employee'].with_context(
        active_test=False, no_wizard=True,
    ).search([
        ('name', '=like', '%' + LEGACY_PLACEHOLDER_SUFFIX),
        ('active', '=', True),
    ])
    if not placeholders:
        return

    # Grab the work contacts before archiving: hr.employee.create() spawns a
    # res.partner per employee, so the same 83 names were also polluting
    # Contacts and every partner picker. Archiving the employee does not
    # cascade to it.
    partners = placeholders.work_contact_id

    placeholders.action_archive()
    partners.filtered('active').action_archive()
