# -*- coding: utf-8 -*-
from odoo import api, fields, models

# Keys on res.users.write() that can change the user's groups.
_USER_GROUP_KEYS = ('group_ids', 'all_group_ids', 'role')
# Keys on res.groups.write() that can change who holds a group, directly
# or through implication.
_GROUP_MEMBER_KEYS = ('user_ids', 'all_user_ids', 'implied_ids', 'implied_by_ids')
# Set while an outer write is already recording, so an inverse or nested
# write inside it is not logged twice.
_CTX_ACTIVE = '_ksw_group_log_active'


class KswUserGroupLog(models.Model):
    """Who gained or lost which access right, when, and who did it.

    Odoo keeps group membership in `res_groups_users_rel`, which has no
    timestamp, so "since when does this user have X?" is unanswerable
    from stock data (Sep 2026: user 1111 / attendance sheet Supervisor).
    This table is that missing history. Rows are only ever created by
    the hooks below, through sudo; nobody can edit or delete them.

    `is_implied` rows record the effective right a user gained or lost
    because a group they hold implies it (e.g. attendance sheet Manager
    implies Supervisor), so the answer is the same whichever route the
    right came through.
    """
    _name = 'ksw.user.group.log'
    _description = 'User Access Right Change'
    _order = 'id desc'

    date = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    user_id = fields.Many2one('res.users', string='User', required=True, readonly=True,
                              ondelete='cascade', index=True)
    group_id = fields.Many2one('res.groups', string='Access Right', required=True, readonly=True,
                               ondelete='cascade', index=True)
    action = fields.Selection([('added', 'Granted'), ('removed', 'Revoked')],
                              required=True, readonly=True)
    is_implied = fields.Boolean(string='Implied', readonly=True,
                                help='Granted or revoked through another group that implies it, '
                                     'not ticked directly on the user.')
    changed_by_id = fields.Many2one('res.users', string='Changed By', readonly=True,
                                    ondelete='set null')
    source = fields.Selection([
        ('user', 'User form'),
        ('group', 'Group form'),
        ('create', 'User created'),
        ('module', 'Module install/upgrade'),
    ], readonly=True)

    @api.model
    def _snapshot(self, user_ids):
        """{uid: (direct group ids, effective group ids)} straight from the DB."""
        if not user_ids:
            return {}
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT uid, gid FROM res_groups_users_rel WHERE uid IN %s', [tuple(user_ids)])
        direct = {uid: set() for uid in user_ids}
        for uid, gid in self.env.cr.fetchall():
            direct[uid].add(gid)
        Groups = self.env['res.groups'].sudo().with_context(active_test=False)
        implied = {}
        snap = {}
        for uid, gids in direct.items():
            effective = set()
            for gid in gids:
                if gid not in implied:
                    implied[gid] = set(Groups.browse(gid).all_implied_ids.ids)
                effective |= implied[gid]
            snap[uid] = (gids, effective | gids)
        return snap

    @api.model
    def _record(self, before, after, source):
        if self.env.context.get('install_mode') or self.env.context.get('module'):
            source = 'module'
        vals = []
        for uid in set(before) | set(after):
            b_direct, b_eff = before.get(uid, (set(), set()))
            a_direct, a_eff = after.get(uid, (set(), set()))
            for gid in a_eff - b_eff:
                vals.append(self._vals(uid, gid, 'added', gid not in a_direct, source))
            for gid in b_eff - a_eff:
                vals.append(self._vals(uid, gid, 'removed', gid not in b_direct, source))
        if vals:
            self.sudo().create(vals)

    @api.model
    def _vals(self, uid, gid, action, is_implied, source):
        return {
            'user_id': uid, 'group_id': gid, 'action': action,
            'is_implied': is_implied, 'source': source,
            'changed_by_id': self.env.uid,
        }


class ResUsers(models.Model):
    _inherit = 'res.users'

    x_group_log_ids = fields.One2many('ksw.user.group.log', 'user_id',
                                      string='Access Right History', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_CTX_ACTIVE):
            return super().create(vals_list)
        users = super(ResUsers, self.with_context(**{_CTX_ACTIVE: True})).create(vals_list)
        Log = self.env['ksw.user.group.log']
        Log._record({}, Log._snapshot(users.ids), 'create')
        return users.with_env(self.env)

    def write(self, vals):
        if self.env.context.get(_CTX_ACTIVE) or not self.ids or not any(
                k in vals for k in _USER_GROUP_KEYS):
            return super().write(vals)
        Log = self.env['ksw.user.group.log']
        before = Log._snapshot(self.ids)
        res = super(ResUsers, self.with_context(**{_CTX_ACTIVE: True})).write(vals)
        self.env.invalidate_all()
        Log._record(before, Log._snapshot(self.ids), 'user')
        return res


class ResGroups(models.Model):
    _inherit = 'res.groups'

    def write(self, vals):
        if self.env.context.get(_CTX_ACTIVE) or not self.ids or not any(
                k in vals for k in _GROUP_MEMBER_KEYS):
            return super().write(vals)
        Log = self.env['ksw.user.group.log']
        # Affected: anyone holding this group (or a group implying it), any
        # user named in the write, and the holders of any newly-implying
        # group. The before-snapshot must cover all of them, or a user just
        # added would have every group he already held logged as new.
        groups = self.sudo().with_context(active_test=False)
        uids = set(groups.all_user_ids.ids)
        for key in ('user_ids', 'all_user_ids'):
            uids |= _command_ids(vals.get(key))
        implying = self.env['res.groups'].sudo().with_context(active_test=False).browse(
            _command_ids(vals.get('implied_by_ids'))).exists()
        uids |= set(implying.all_user_ids.ids)
        before = Log._snapshot(uids)
        res = super(ResGroups, self.with_context(**{_CTX_ACTIVE: True})).write(vals)
        self.env.invalidate_all()
        uids |= set(groups.all_user_ids.ids)
        extra = uids - set(before)
        if extra:
            # Created inline by a (0, 0, vals) command: they had nothing before.
            before.update({uid: (set(), set()) for uid in extra})
        Log._record(before, Log._snapshot(uids), 'group')
        return res


def _command_ids(value):
    """Record ids referenced by an x2many write value (ids or commands)."""
    ids = set()
    for cmd in value or ():
        if isinstance(cmd, int):
            ids.add(cmd)
        elif isinstance(cmd, (list, tuple)) and cmd:
            if cmd[0] in (1, 2, 3, 4) and len(cmd) > 1:
                ids.add(cmd[1])
            elif cmd[0] == 6 and len(cmd) > 2:
                ids.update(cmd[2] or ())
    return ids
