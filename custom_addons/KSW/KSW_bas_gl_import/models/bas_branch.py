from odoo import api, fields, models


class BasBranch(models.Model):
    """A BAS ``CODE2``, under the name people actually use.

    BAS has no branch master: ``CODE2`` is only a number on each voucher.
    The names here were worked out from the cash box and the sales accounts
    each code posts to (``COD10.DNAME``, 2026-10-21) and can be corrected
    by hand. A new code met during an import is added under its number so
    nothing is ever left without a branch.
    """
    _name = 'ksw.bas.branch'
    _description = 'BAS Branch'
    _order = 'code'
    _rec_name = 'name'

    code = fields.Char(required=True, index=True,
                       help='BAS CODE2 on vouchers and delivery notes.')
    name = fields.Char(required=True, translate=True)
    note = fields.Char(string='How it was identified')
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint('UNIQUE (code)', 'This BAS branch code already exists.')

    @api.model
    def _for_codes(self, codes):
        """{code: branch} for ``codes``, creating any code not seen before."""
        codes = {(c or '').strip() for c in codes} - {''}
        if not codes:
            return {}
        branches = self.sudo().with_context(active_test=False).search(
            [('code', 'in', list(codes))])
        found = {b.code: b for b in branches}
        missing = sorted(codes - set(found))
        # Not while the table is still empty: on the upgrade that adds this
        # model, existing records are computed before data/bas_branch_data.xml
        # loads, and a "120" made here would collide with the seeded Nabiya.
        # The migration links those records once the branches exist.
        if missing and self.sudo().with_context(active_test=False).search_count([], limit=1):
            for b in self.sudo().create([{'code': c, 'name': c} for c in missing]):
                found[b.code] = b
        return found


class BasBranchMixin(models.AbstractModel):
    _name = 'ksw.bas.branch.mixin'
    _description = 'Has a BAS branch'

    x_bas_branch = fields.Char(string='BAS Branch Code', index=True, copy=False)
    x_bas_branch_id = fields.Many2one(
        'ksw.bas.branch', string='Branch', compute='_compute_bas_branch_id',
        store=True, index=True)

    @api.depends('x_bas_branch')
    def _compute_bas_branch_id(self):
        branches = self.env['ksw.bas.branch']._for_codes(self.mapped('x_bas_branch'))
        for rec in self:
            rec.x_bas_branch_id = branches.get((rec.x_bas_branch or '').strip(), False)
