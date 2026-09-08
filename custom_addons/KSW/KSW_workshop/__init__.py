from . import models

from .models.ksw_workshop_client import _register_existing_clients


def _post_init_hook(env):
    """Seed the workshop client registry on a fresh install.

    Shares its implementation with migrations/19.0.8.0.0/post-migrate.py, so
    install and upgrade cannot drift — the arrangement KSW_fleet already uses
    for its customer_rank stamping.
    """
    _register_existing_clients(env)
