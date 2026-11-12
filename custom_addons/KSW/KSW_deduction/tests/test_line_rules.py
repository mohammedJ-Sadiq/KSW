# -*- coding: utf-8 -*-
"""Installment lines follow their deduction (security audit 2026-11-12).

ksw.deduction.line had no record rule: every internal user could read every
employee's installments. These pin the mirror rules in line_rules.xml.
"""
from odoo.tests.common import tagged

from .common import DeductionCommon


@tagged('post_install', '-at_install')
class TestLineRules(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        user_group = cls.env.ref('base.group_user')

        def make_user(login, employee):
            user = cls.env['res.users'].create({
                'name': login, 'login': login,
                'group_ids': [(6, 0, [user_group.id])]})
            employee.user_id = user
            return user

        cls.owner_user = make_user('ksw_line_owner', cls.employee)
        cls.manager_user = make_user('ksw_line_manager', cls.manager_emp)
        cls.outsider_user = make_user('ksw_line_outsider', cls.employee_b)
        cls.officer_user = cls.env['res.users'].create({
            'name': 'ksw_line_officer', 'login': 'ksw_line_officer',
            'group_ids': [(6, 0, [
                user_group.id,
                cls.env.ref('KSW_deduction.group_deduction_officer').id])]})

    def setUp(self):
        super().setUp()
        self.ded = self._activate(self._make_deduction(amount=900.0,
                                                       installments=3))
        self.lines = self.ded.line_ids
        self.assertTrue(self.lines, 'the deduction generated no installments')

    def _visible(self, user):
        return self.env['ksw.deduction.line'].with_user(user).search(
            [('id', 'in', self.lines.ids)])

    def test_owner_and_manager_see_the_installments(self):
        self.assertEqual(self._visible(self.owner_user), self.lines)
        self.assertEqual(self._visible(self.manager_user), self.lines)

    def test_outsider_sees_none(self):
        self.assertFalse(self._visible(self.outsider_user))

    def test_officer_sees_all(self):
        self.assertEqual(self._visible(self.officer_user), self.lines)

    def test_lines_never_wider_than_their_deduction(self):
        """For every test user: visible lines == lines of visible
        deductions, so a future READ rule on ksw.deduction without a twin
        in line_rules.xml shows up here."""
        for user in (self.owner_user, self.manager_user, self.outsider_user,
                     self.officer_user):
            deds = self.env['ksw.deduction'].with_user(user).search([])
            lines = self.env['ksw.deduction.line'].with_user(user).search([])
            self.assertEqual(lines.sudo().deduction_id <= deds.sudo(), True,
                             '%s reads installments of deductions it cannot '
                             'see' % user.login)
