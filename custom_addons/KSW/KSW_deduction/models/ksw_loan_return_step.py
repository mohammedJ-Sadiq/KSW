from odoo import fields, models


class KswLoanReturnStep(models.Model):
    """The steps a loan request can be sent back to, as records.

    This exists only so the return wizard can offer a *per-record* list. A
    Selection whose options are narrowed by a dynamic ``selection=`` method
    cannot work in a dialog: the web client strips everything but ``lang``
    and ``*_view_ref`` out of the context before calling ``get_views`` (see
    ``view_service.js::loadViews``) and then caches the payload on disk per
    model, so the method never learns which loan is open and every wizard
    reuses the first answer (KSW_annual_leave hit this — see
    ``ksw.leave.return.step`` for the original fix).

    A Many2one is evaluated differently: ``radio_field`` resolves its domain
    against the record in front of it, so a domain of
    ``[('id', 'in', allowed_step_ids)]`` is always right for the loan being
    returned.
    """
    _name = 'ksw.loan.return.step'
    _description = 'Loan Request Return Target'
    _order = 'sequence, id'

    code = fields.Char(
        required=True, index=True,
        help='Matches the ksw.deduction.approval_state value this step '
             'returns to.',
    )
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)

    _code_uniq = models.Constraint(
        'UNIQUE(code)',
        'Each return step must have a unique code.',
    )
