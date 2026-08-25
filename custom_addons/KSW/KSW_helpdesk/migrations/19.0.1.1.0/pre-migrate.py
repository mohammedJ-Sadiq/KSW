def migrate(cr, version):
    # The category uniqueness constraint moved from unique(name) to
    # unique(name, ticket_type) so Incident and Service Request categories
    # can share a name (e.g. both have an "Other" catch-all). Renaming the
    # models.Constraint attribute does not drop the old SQL constraint, so
    # drop it explicitly before the new schema/data load runs.
    cr.execute("""
        ALTER TABLE helpdesk_ticket_category
        DROP CONSTRAINT IF EXISTS helpdesk_ticket_category_name_uniq
    """)
