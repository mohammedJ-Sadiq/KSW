from odoo import api, fields, models


class HrPayslipInput(models.Model):
    """Record which months a multi-month advance actually pays for.

    A vacation payslip pays the housing allowance (and GOSI) for several
    months at once, as a single ``VACATION_HRA`` / ``VACATION_GOSI`` lump.
    The ordinary monthly payslip for one of those months has to know how
    much of that lump was its own, or it pays the allowance a second time
    (``hr.payslip._inject_prior_hra_input``).

    The lump alone does not answer that: it is *not* simply the leave's
    span divided by its months — months the employee was already paid for
    are dropped from the advance
    (``hr.leave._months_already_settled``), so a two-month leave can
    carry a one-month advance.  Only the months that went into the figure
    can say, so they are written down here when it is built.
    """
    _inherit = 'hr.payslip.input'

    x_ksw_advance_months = fields.Char(
        string='Advance Covers',
        copy=False,
        help='Months a multi-month advance pays for, as "YYYY-MM,YYYY-MM". '
             'Read by the monthly payslip of one of those months to work '
             'out how much of the lump belongs to it, so the allowance is '
             'not paid twice.',
    )

    @api.model
    def _ksw_format_advance_months(self, months):
        """``[(2026, 7), (2026, 8)]`` → ``'2026-07,2026-08'``."""
        return ','.join('%04d-%02d' % (y, m) for y, m in sorted(months))

    def _ksw_advance_month_list(self):
        """``'2026-07,2026-08'`` → ``[(2026, 7), (2026, 8)]``.

        Unparseable entries are dropped rather than raised on: this value
        only ever narrows a double-payment guard, and a payslip must not
        fail to compute over it.
        """
        self.ensure_one()
        months = []
        for chunk in (self.x_ksw_advance_months or '').split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                year, month = chunk.split('-')
                months.append((int(year), int(month)))
            except ValueError:
                continue
        return months
