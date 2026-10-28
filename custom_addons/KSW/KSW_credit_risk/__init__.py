from . import models


def _post_init_hook(env):
    # Prefill every BAS-linked customer at install; the 10-minute BAS
    # customer sync keeps it current from then on.
    env['ksw.bas.customer']._apply_risk_grace()
