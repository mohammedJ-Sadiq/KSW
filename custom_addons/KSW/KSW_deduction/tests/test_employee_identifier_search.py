# -*- coding: utf-8 -*-
"""Employee lookup by SSN / employee no. / loan account for deduction roles.

The accounting data-entry team is not part of hr.group_hr_user, so the
identifier fields listed in `KSW_payroll`'s `_rec_names_search` resolve to
nothing for them (`ssnid` / `identification_id` live on hr.version, whose
record rules only expose the user's own version; `x_employee_no` and
`x_loan_acc_no` are group-gated). Both `hr.employee` and
`hr.employee.public` therefore override `_search_display_name` — the public
model matters because `hr.employee.search_fetch` delegates to it for any
user without model-level read access on hr.employee.
"""
from .common import DeductionCommon


class TestEmployeeIdentifierSearch(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee.sudo().write({
            'ssnid': '1099887766',
            'identification_id': 'IQ-55443322',
            'x_employee_no': 'KSW-EMP-4711',
            'x_loan_acc_no': 'BAS-777001',
        })
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _mk(login, group_xmlids):
            return Users.create({
                'name': login,
                'login': login,
                'email': f'{login}@kswsearch.test',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in group_xmlids])],
            })

        cls.user_data_entry = _mk(
            'kswsr_data_entry', ['KSW_deduction.group_acc_data_entry'])
        cls.user_loan_acc = _mk(
            'kswsr_loan_acc', ['KSW_deduction.group_loan_acc'])
        cls.user_plain = _mk(
            'kswsr_plain', ['KSW_deduction.group_deduction_user'])

    def _found(self, user, term):
        return [
            rec_id for rec_id, _name
            in self.env['hr.employee'].with_user(user).name_search(term)
        ]

    def test_data_entry_is_not_an_hr_user(self):
        """Guard: the whole point of the override is this role's lack of
        hr.group_hr_user — if that ever changes these tests are moot."""
        self.assertFalse(self.user_data_entry.has_group('hr.group_hr_user'))

    def test_data_entry_finds_employee_by_ssn(self):
        self.assertIn(self.employee.id,
                      self._found(self.user_data_entry, '1099887766'))

    def test_data_entry_finds_employee_by_identification_id(self):
        self.assertIn(self.employee.id,
                      self._found(self.user_data_entry, 'IQ-55443322'))

    def test_data_entry_finds_employee_by_employee_no(self):
        self.assertIn(self.employee.id,
                      self._found(self.user_data_entry, 'KSW-EMP-4711'))

    def test_data_entry_finds_employee_by_loan_account(self):
        self.assertIn(self.employee.id,
                      self._found(self.user_data_entry, 'BAS-777001'))

    def test_data_entry_still_finds_employee_by_name(self):
        self.assertIn(self.employee.id,
                      self._found(self.user_data_entry, 'KSWDED Employee'))

    def test_loan_approver_finds_employee_by_ssn(self):
        """Every deduction role gets the widened lookup, not just data entry."""
        self.assertIn(self.employee.id,
                      self._found(self.user_loan_acc, '1099887766'))

    def test_plain_user_cannot_find_employee_by_ssn(self):
        """The widening is scoped to the deduction roles — a plain internal
        user must not gain an SSN lookup on every colleague."""
        self.assertNotIn(self.employee.id,
                         self._found(self.user_plain, '1099887766'))

    def test_hr_user_path_unchanged(self):
        """HR users keep the standard `_rec_names_search` behaviour."""
        self.assertIn(self.employee.id,
                      [rec_id for rec_id, _n
                       in self.env['hr.employee'].name_search('1099887766')])

    def test_no_match_returns_empty(self):
        self.assertEqual(
            self._found(self.user_data_entry, 'NOSUCHIDENTIFIER-0000'), [])
