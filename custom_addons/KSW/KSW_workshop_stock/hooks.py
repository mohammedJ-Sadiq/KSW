def post_init_hook(env):
    """Link vehicles to their BAS cost centres on install."""
    env['ksw.fleet.vehicle']._ksw_match_cost_centres()
