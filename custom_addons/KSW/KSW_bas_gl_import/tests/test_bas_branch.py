from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBasBranch(TransactionCase):
    """A BAS CODE2 reads as its branch name, never as the number."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'general'), ('company_id', '=', cls.env.company.id)], limit=1)

    def _move(self, code):
        return self.env['account.move'].create({
            'journal_id': self.journal.id, 'x_bas_branch': code})

    def test_seeded_code_shows_the_branch_name(self):
        move = self._move('120')
        self.assertEqual(move.x_bas_branch_id,
                         self.env.ref('KSW_bas_gl_import.bas_branch_120'))
        self.assertEqual(move.x_bas_branch_id.with_context(lang='en_US').name, 'Nabiya')

    def test_renaming_a_branch_relabels_its_records(self):
        move = self._move('190')
        move.x_bas_branch_id.name = 'Haradh Factory'
        self.assertEqual(move.x_bas_branch_id.name, 'Haradh Factory')

    def test_an_unknown_code_gets_a_branch_to_be_named(self):
        move = self._move('999')
        self.assertEqual(move.x_bas_branch_id.code, '999')
        self.assertEqual(move.x_bas_branch_id.name, '999')

    def test_a_delivery_note_groups_by_branch(self):
        picking_type = self.env['stock.picking.type'].search(
            [('code', '=', 'outgoing'), ('company_id', '=', self.env.company.id)], limit=1)
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id, 'x_bas_branch': '172'})
        groups = self.env['stock.picking'].read_group(
            [('id', '=', picking.id)], ['x_bas_branch_id'], ['x_bas_branch_id'])
        self.assertEqual(groups[0]['x_bas_branch_id'][0],
                         self.env.ref('KSW_bas_gl_import.bas_branch_172').id)
