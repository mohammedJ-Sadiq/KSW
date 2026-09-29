# -*- coding: utf-8 -*-
from odoo import Command
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestUserGroupLog(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Groups = cls.env['res.groups']
        cls.g_child = Groups.create({'name': 'KSW Log Test Supervisor'})
        cls.g_parent = Groups.create({
            'name': 'KSW Log Test Manager',
            'implied_ids': [Command.link(cls.g_child.id)],
        })
        cls.g_other = Groups.create({'name': 'KSW Log Test Other'})
        cls.user = cls.env['res.users'].create({
            'name': 'Log Test User', 'login': 'ksw_log_test_user',
            'group_ids': [Command.link(cls.env.ref('base.group_user').id)],
        })

    def _logs(self, group, **kw):
        domain = [('user_id', '=', self.user.id), ('group_id', '=', group.id)]
        domain += [(k, '=', v) for k, v in kw.items()]
        return self.env['ksw.user.group.log'].search(domain)

    def test_user_form_grant_and_revoke(self):
        self.user.write({'group_ids': [Command.link(self.g_child.id)]})
        log = self._logs(self.g_child, action='added')
        self.assertEqual(len(log), 1)
        self.assertFalse(log.is_implied)
        self.assertEqual(log.source, 'user')
        self.assertEqual(log.changed_by_id, self.env.user)

        self.user.write({'group_ids': [Command.unlink(self.g_child.id)]})
        self.assertEqual(len(self._logs(self.g_child, action='removed')), 1)

    def test_implied_grant_is_logged(self):
        self.user.write({'group_ids': [Command.link(self.g_parent.id)]})
        self.assertTrue(self._logs(self.g_parent, action='added', is_implied=False))
        self.assertTrue(self._logs(self.g_child, action='added', is_implied=True))

    def test_group_form_add_does_not_relog_existing_groups(self):
        self.user.write({'group_ids': [Command.link(self.g_other.id)]})
        self.g_child.write({'user_ids': [Command.link(self.user.id)]})
        self.assertEqual(len(self._logs(self.g_child, action='added', source='group')), 1)
        # A group the user already held must not reappear as a new grant.
        self.assertEqual(len(self._logs(self.g_other, action='added')), 1)

    def test_unrelated_write_logs_nothing(self):
        Log = self.env['ksw.user.group.log']
        before = Log.search_count([('user_id', '=', self.user.id)])
        self.user.write({'name': 'Renamed'})
        self.user.write({'group_ids': [Command.link(self.env.ref('base.group_user').id)]})
        self.assertEqual(Log.search_count([('user_id', '=', self.user.id)]), before)

    def test_implication_change_on_group(self):
        self.user.write({'group_ids': [Command.link(self.g_other.id)]})
        self.g_other.write({'implied_ids': [Command.link(self.g_child.id)]})
        self.assertTrue(self._logs(self.g_child, action='added', is_implied=True, source='group'))
