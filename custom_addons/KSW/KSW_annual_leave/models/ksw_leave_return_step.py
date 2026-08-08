from odoo import fields, models


class KswLeaveReturnStep(models.Model):
    """The steps a request can be sent back to, as records.

    This exists only so the return wizard can offer a *per-record* list. The
    obvious implementation — a Selection whose options are narrowed by a
    dynamic ``selection=`` method — cannot work in a dialog: the web client
    strips everything but ``lang`` and ``*_view_ref`` out of the context
    before calling ``get_views`` (see ``view_service.js::loadViews``) and then
    caches the payload on disk per model, so the method never learns which
    leave is open and every wizard reuses the first answer.

    A Many2one is evaluated differently: ``radio_field`` resolves its domain
    against the record in front of it (``getFieldDomain(props.record, ...)``),
    so a domain of ``[('id', 'in', allowed_step_ids)]`` is always right for
    the request being returned.
    """
    _name = 'ksw.leave.return.step'
    _description = 'Time Off Return Target'
    _order = 'sequence, id'

    code = fields.Char(
        required=True, index=True,
        help='Matches the x_annual_approval_state value this step returns to '
             "(or 'confirm' for leave types with no KSW chain).",
    )
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)

    # `code` is the join back to x_annual_approval_state, so a second record
    # carrying the same code silently doubles every option in the dialog. That
    # is not hypothetical: running the module once without its data file (a
    # stashed baseline) orphaned the first set from ir.model.data and the next
    # load created a second one alongside it.
    _code_uniq = models.Constraint(
        'UNIQUE(code)',
        'Each return step must have a unique code.',
    )
