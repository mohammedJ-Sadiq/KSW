"""Importing driver trips when not every driver is in BAS.

The importer used to write a zero-quantity entry for any driver it could
not find in BAS. ``ksw.pay.entry._check_quantity`` rejects that, so the
ValidationError rolled back the *whole* import: one driver with no BAS
cost centre cost every other driver at the site his trips, and the error
named nobody. These tests pin the behaviour that replaced it — skip the
driver, import the rest, name who was skipped.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class BasTripsImportCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2028-07-01'
        cls.site = env['ksw.site'].create({
            'name': 'BAS Site', 'code': 'BS',
            'required_trips_full_month': 50,
        })
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')

        # Matched: cost centre set, and BAS has loads for it.
        cls.driver_ok = cls._driver('Driver Matched', 'WAHAB JAN1387')
        # Cost centre set, but BAS returns nothing for it this month.
        cls.driver_no_data = cls._driver('Driver No Data', 'GHOST DRIVER999')
        # Never mapped at all.
        cls.driver_unmapped = cls._driver('Driver Unmapped', False)

        cls.bas_rows = {
            'wahab jan1387': {
                'loads': 118, 'mult': 161.23,
                'equip': 'T166', 'raw_cc': 'WAHAB JAN1387',
            },
        }

    @classmethod
    def _driver(cls, name, cost_center):
        return cls.env['hr.employee'].sudo().create({
            'name': name,
            'x_site_id': cls.site.id,
            'x_bas_driver_cost_center': cost_center,
        })

    def _batch(self):
        return self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.c_trips.id,
            'period': self.period,
            'site_id': self.site.id,
        })

    def _import(self, batch, rows=None):
        """Run the importer with BAS stubbed out."""
        rows = self.bas_rows if rows is None else rows
        with patch.object(
            type(batch), '_bas_fetch_orood', return_value=rows
        ):
            return batch._import_bas_trips()

    @staticmethod
    def _message(result):
        return result['params']['message']


class TestBasTripsImport(BasTripsImportCommon):

    def test_01_unmapped_driver_does_not_roll_back_the_import(self):
        """The matched driver is imported even though two others are not."""
        batch = self._batch()
        result = self._import(batch)

        self.assertEqual(len(batch.entry_ids), 1)
        entry = batch.entry_ids
        self.assertEqual(entry.employee_id, self.driver_ok)
        self.assertAlmostEqual(entry.quantity, 161.23, places=2)
        self.assertAlmostEqual(entry.quantity_ref, 118.0, places=2)
        self.assertIn('1 driver(s) filled from BAS', self._message(result))

    def test_02_skipped_drivers_are_named(self):
        """The summary says who was skipped, and why — the old error did not."""
        batch = self._batch()
        message = self._message(self._import(batch))

        self.assertIn('no BAS cost centre set', message)
        self.assertIn('Driver Unmapped', message)
        self.assertIn('had no trips in BAS this month', message)
        self.assertIn('Driver No Data', message)
        # The matched driver is not reported as a problem.
        self.assertNotIn('Driver Matched', message)

    def test_03_zero_quantity_entry_is_never_created(self):
        """No entry at all for a skipped driver, rather than a 0 one."""
        batch = self._batch()
        self._import(batch)

        self.assertNotIn(
            self.driver_no_data, batch.entry_ids.mapped('employee_id'))
        self.assertNotIn(
            self.driver_unmapped, batch.entry_ids.mapped('employee_id'))
        self.assertFalse(batch.entry_ids.filtered(lambda e: e.quantity <= 0))

    def test_04_no_matches_at_all_reports_instead_of_raising(self):
        """An empty BAS month is a message, not a failed button."""
        batch = self._batch()
        message = self._message(self._import(batch, rows={}))

        self.assertFalse(batch.entry_ids)
        self.assertIn('0 driver(s) filled from BAS', message)

    def test_05_existing_line_of_a_skipped_driver_is_left_alone(self):
        """A figure already in the batch is not silently destroyed."""
        batch = self._batch()
        existing = self.env['ksw.pay.entry'].sudo().create({
            'batch_id': batch.id,
            'employee_id': self.driver_no_data.id,
            'quantity': 42.0,
        })
        message = self._message(self._import(batch))

        self.assertTrue(existing.exists())
        self.assertAlmostEqual(existing.quantity, 42.0, places=2)
        self.assertIn('left unchanged', message)
        self.assertIn('Driver No Data', message)

    def test_06_rerun_updates_the_matched_line_in_place(self):
        """Importing twice does not duplicate the matched driver."""
        batch = self._batch()
        self._import(batch)
        first = batch.entry_ids
        self._import(batch, rows={
            'wahab jan1387': {
                'loads': 120, 'mult': 170.0,
                'equip': 'T166', 'raw_cc': 'WAHAB JAN1387',
            },
        })

        self.assertEqual(len(batch.entry_ids), 1)
        self.assertEqual(batch.entry_ids, first)
        self.assertAlmostEqual(batch.entry_ids.quantity, 170.0, places=2)

    def test_07_import_refreshes_the_form(self):
        """The toast chains into a reload, or the Entries tab stays stale.

        A button that returns an action does not get the web client's
        automatic record reload, so the imported lines would sit unseen in
        the database until the user refreshed the page by hand.
        """
        batch = self._batch()
        result = self._import(batch)

        self.assertEqual(
            result['params'].get('next'),
            {'type': 'ir.actions.client', 'tag': 'soft_reload'})

    def test_08_threshold_is_prorated_to_the_site_allowance(self):
        """No attendance sheet -> the full-month allowance, not zero."""
        batch = self._batch()
        self._import(batch)

        self.assertAlmostEqual(batch.entry_ids.threshold_qty, 50.0, places=2)


class TestNameList(BasTripsImportCommon):

    def test_09_long_name_list_is_truncated_with_a_count(self):
        batch = self._batch()
        extras = self.env['hr.employee'].sudo().browse()
        for i in range(7):
            extras |= self._driver('Extra %s' % i, False)

        message = self._message(self._import(batch))
        self.assertIn('and 3 more', message)
