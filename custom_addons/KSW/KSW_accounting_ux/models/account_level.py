from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain

# BAS's chart: 3 / 33 / 3302 / 330201 are account.group (levels 1-4), the
# leaf accounts are level 5.
LEVELS = (1, 2, 3, 4)
LEVEL_FIELDS = tuple(f'x_group_l{n}_id' for n in LEVELS)


class AccountAccount(models.Model):
    _inherit = 'account.account'

    x_group_l1_id = fields.Many2one('account.group', 'Level 1', compute='_compute_ksw_group_levels', store=True)
    x_group_l2_id = fields.Many2one('account.group', 'Level 2', compute='_compute_ksw_group_levels', store=True)
    x_group_l3_id = fields.Many2one('account.group', 'Level 3', compute='_compute_ksw_group_levels', store=True)
    x_group_l4_id = fields.Many2one('account.group', 'Level 4', compute='_compute_ksw_group_levels', store=True)

    @api.depends('code_store')
    def _compute_ksw_group_levels(self):
        """Each ancestor group of the account, root first.

        ``group_id`` is not stored (core resolves it from the code prefix),
        so nothing can group by it; these are, and account.move.line
        carries them so every journal-item list can.
        """
        # group_id resolves in one query per company for a whole recordset;
        # record by record it costs ~8 queries per account.
        # core's group_id has no dependency on account.group: drop a value
        # cached before a group was added or moved.
        self.invalidate_recordset(['group_id'])
        leaf = {}
        for company in self.company_ids:
            batch = self.filtered(lambda a: a.company_ids[:1] == company)
            for account in batch.with_company(company):
                leaf[account.id] = account.group_id
        # Assign per distinct value, not per account: every assignment walks
        # the dependent journal items, so 4,700 accounts x 4 levels one by
        # one cost ~38k queries and a minute.
        by_value = defaultdict(list)
        for account in self:
            chain, group = [], leaf.get(account.id) or self.env['account.group']
            while group:
                chain.insert(0, group)
                group = group.parent_id
            for n, fname in zip(LEVELS, LEVEL_FIELDS):
                by_value[fname, chain[n - 1].id if len(chain) >= n else False].append(account.id)
        for (fname, group_id), ids in by_value.items():
            self.browse(ids)[fname] = group_id

    def _ksw_level_group_by(self):
        """``group_by`` that opens ``self`` one level below what they share.

        Accounts all under 3302 -> [Level 4, Account]; under 41 -> [Level 3,
        Level 4, Account], the way BAS drills. A single account: none.
        """
        if len(self) <= 1:
            return []
        shared = 0
        for n, fname in zip(LEVELS, LEVEL_FIELDS):
            values = set(self.mapped(fname))
            if len(values) != 1 or not next(iter(values)):
                break
            shared = n
        return [f for n, f in zip(LEVELS, LEVEL_FIELDS)
                if n > shared and any(self.mapped(f))] + ['account_id']


class AccountGroup(models.Model):
    _inherit = 'account.group'

    def _ksw_affected_accounts(self):
        """Accounts inside these groups' code ranges, or pointing at them."""
        Account = self.env['account.account'].sudo().with_context(active_test=False)
        domain = Domain.OR(
            [Domain(f, 'in', self.ids) for f in LEVEL_FIELDS]
            + [Domain('code', '>=', g.code_prefix_start) & (
                   Domain('code', '<=', g.code_prefix_end or g.code_prefix_start)
                   | Domain('code', '=like', (g.code_prefix_end or g.code_prefix_start) + '%'))
               for g in self if g.code_prefix_start])
        return Account.search(domain)

    @api.model_create_multi
    def create(self, vals_list):
        groups = super().create(vals_list)
        groups._ksw_affected_accounts()._compute_ksw_group_levels()
        return groups

    def write(self, vals):
        tracked = {'code_prefix_start', 'code_prefix_end', 'parent_id', 'company_id'}
        if not tracked & set(vals):
            return super().write(vals)
        before = self._ksw_affected_accounts()
        res = super().write(vals)
        (before | self._ksw_affected_accounts())._compute_ksw_group_levels()
        return res

    def unlink(self):
        affected = self._ksw_affected_accounts()
        res = super().unlink()
        affected.exists()._compute_ksw_group_levels()
        return res


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    x_group_l1_id = fields.Many2one(related='account_id.x_group_l1_id', store=True)
    x_group_l2_id = fields.Many2one(related='account_id.x_group_l2_id', store=True)
    x_group_l3_id = fields.Many2one(related='account_id.x_group_l3_id', store=True)
    x_group_l4_id = fields.Many2one(related='account_id.x_group_l4_id', store=True)
