from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasDocumentNumber(TransactionCase):
    """BAS Document No. names the branch, and still finds "155/15401"."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['ksw.bas.branch'].create(
            {'code': '9991', 'name': 'External Sales (Drivers)'})
        cls.branch.update_field_translations('name', {'ar_001': 'المبيعات الخارجية (السائقين)'})
        cls.move = cls.env['account.move'].create({
            'move_type': 'entry', 'x_bas_key': '002/3/9991/15401', 'x_bas_branch': '9991'})

    def test_shows_branch_name_not_code(self):
        self.assertEqual(self.move.x_bas_number, 'External Sales (Drivers) / 15401')
        if self.env['res.lang']._lang_get('ar_001'):
            self.assertEqual(self.move.with_context(lang='ar_001').x_bas_number,
                             'المبيعات الخارجية (السائقين) / 15401')

    def test_search_by_code_number_or_name(self):
        Move = self.env['account.move']
        for term in ('9991/15401', '15401', 'External Sales', 'External Sales (Drivers) / 15401'):
            self.assertIn(self.move, Move.search([('x_bas_number', 'ilike', term)]), term)
        self.assertNotIn(self.move, Move.search([('x_bas_number', 'ilike', '9991/15402')]))
