def migrate(cr, version):
    # Roles collapsed from three (User / Agent / Manager) to two
    # (User / IT Team - the former group_helpdesk_agent xmlid, kept as-is
    # to avoid touching every Python/view reference). group_helpdesk_manager
    # is being removed from security.xml entirely, so the normal
    # orphan ir.model.data cleanup will delete it once this upgrade
    # finishes loading data - which would silently drop its members from
    # any IT role. Move them into group_helpdesk_agent first so nobody
    # loses access.
    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'KSW_helpdesk' AND name = 'group_helpdesk_manager'
    """)
    row = cr.fetchone()
    if not row:
        return
    manager_group_id = row[0]

    cr.execute("""
        SELECT res_id FROM ir_model_data
        WHERE module = 'KSW_helpdesk' AND name = 'group_helpdesk_agent'
    """)
    agent_group_id = cr.fetchone()[0]

    cr.execute("""
        INSERT INTO res_groups_users_rel (gid, uid)
        SELECT %s, uid FROM res_groups_users_rel WHERE gid = %s
        ON CONFLICT DO NOTHING
    """, (agent_group_id, manager_group_id))
