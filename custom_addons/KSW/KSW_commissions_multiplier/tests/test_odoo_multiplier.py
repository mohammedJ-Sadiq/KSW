"""Driver trips on the Odoo «الرد المضاعف», and back-dated changes re-weighed.

BAS is stubbed at two points: the base per-driver query (the KSW_commissions
class, so this module's override still runs on top of it) and the per-load
query this module adds.
"""
from datetime import date
from unittest.mock import patch

from freezegun import freeze_time

from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.KSW_commissions.models.ksw_pay_import_bas import (
    KswPayBatchBasImport,
)
from ..models.ksw_pay_import_bas import KswPayBatchOdooMultiplier

JUL = date(2028, 7, 1)
AUG = date(2028, 8, 1)
CODE = '1203ZZ900'
OTHER = '1203ZZ901'


@tagged('post_install', '-at_install')
class TestOdooMultiplier(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.supervisor = new_test_user(
            env, 'cm_supervisor',
            groups='base.group_user,KSW_water_delivery.group_water_dispatcher')
        cls.gm = new_test_user(env, 'cm_gm', groups='base.group_user')
        Emp = env['hr.employee'].sudo()
        cls.gm_emp = Emp.create({'name': 'CM GM', 'user_id': cls.gm.id})
        cls.dept = env['hr.department'].create(
            {'name': 'CM Drivers', 'x_gm_id': cls.gm_emp.id})
        Emp.create({'name': 'CM Supervisor', 'user_id': cls.supervisor.id,
                    'department_id': cls.dept.id})
        cls.driver = Emp.create({'name': 'CM Driver', 'department_id': cls.dept.id,
                                 'x_bas_driver_cost_center': 'CM DRIVER 1'})
        settings = env['ksw.site']._trip_settings()
        settings.sudo().required_trips_full_month = 10
        cls.c_trips = env.ref('KSW_commissions.pay_component_driver_trips')
        cls.c_remainder = env.ref('KSW_commissions.pay_component_gm_extra')

        customer = env['ksw.bas.customer'].create({'bas_code': CODE, 'name_ar': 'عميل الرد'})
        cls.client = env['ksw.client.multiplier'].create({'customer_id': customer.id})
        env['ksw.client.multiplier.rate'].create(
            {'client_id': cls.client.id, 'multiplier': 2.0, 'source': 'opening'})

    # -- BAS stubs ----------------------------------------------------------
    def _bas(self, lines):
        """Stub both queries from one list of loads (driver, code, day, n).

        BAS's own FACTORE is 2.0 for CODE and 1.0 for OTHER: what the base
        query would have summed, and what the swap must take out again."""
        factore = {CODE: 2.0, OTHER: 1.0}
        rows = [{'driver_cc': 'CM DRIVER 1', 'code': c, 'day': d, 'loads': n,
                 'bas_mult': n * factore[c]} for c, d, n in lines]

        def base(batch, date_from, date_to):
            inside = [r for r in rows if date_from <= r['day'] < date_to]
            if not inside:
                return {}
            return {'cm driver 1': {
                'loads': sum(r['loads'] for r in inside),
                'mult': sum(r['bas_mult'] for r in inside),
                'missing': 0, 'new_basis': 0, 'cash_loads': 0,
                'cash_at_till': 0.0, 'cash_at_band': 0.0,
                'equip': 'T1', 'raw_cc': 'CM DRIVER 1'}}

        def loads(batch, date_from, date_to, bas_code=None):
            return [dict(r) for r in rows if date_from <= r['day'] < date_to
                    and (not bas_code or r['code'] == bas_code)]

        p1 = patch.object(KswPayBatchBasImport, '_bas_fetch_orood', base)
        p2 = patch.object(KswPayBatchOdooMultiplier, '_bas_customer_loads', loads)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def _batch(self, component=None, period=JUL):
        return self.env['ksw.pay.batch'].sudo().create({
            'component_id': (component or self.c_trips).id,
            'period': period, 'department_id': self.dept.id})

    def _imported_entry(self):
        batch = self._batch()
        batch._import_bas_trips()
        entry = batch.entry_ids.filtered(lambda e: e.employee_id == self.driver)
        self.assertEqual(len(entry), 1)
        return entry

    def _approve_change(self, new, day):
        req = self.env['ksw.client.multiplier.request'].with_user(self.supervisor).create({
            'client_id': self.client.id, 'new_multiplier': new,
            'effective_date': day, 'reason': 'Moved'})
        req.with_user(self.supervisor).action_submit()
        req.with_user(self.gm).action_approve()
        return req

    # -- the weighting ------------------------------------------------------
    def test_loads_are_weighted_on_the_odoo_history_of_their_day(self):
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': self.client.id, 'multiplier': 3.0,
            'date_from': date(2028, 7, 16)})
        self._bas([(CODE, date(2028, 7, 10), 4), (CODE, date(2028, 7, 20), 2)])
        data = self.env['ksw.pay.batch']._bas_fetch_orood(JUL, AUG)['cm driver 1']
        # 4 × 2.0 before the change + 2 × 3.0 after; BAS would say 6 × 2.0.
        self.assertAlmostEqual(data['mult'], 14.0)
        self.assertEqual(data['loads'], 6)

    def test_a_customer_odoo_has_no_value_for_weighs_nothing_and_is_counted(self):
        self._bas([(CODE, date(2028, 7, 10), 1), (OTHER, date(2028, 7, 10), 3)])
        data = self.env['ksw.pay.batch']._bas_fetch_orood(JUL, AUG)['cm driver 1']
        self.assertAlmostEqual(data['mult'], 2.0)
        self.assertEqual(data['no_odoo_value'], 3)

    def test_days_before_the_odoo_basis_keep_bas(self):
        self._bas([(CODE, date(2026, 9, 2), 2)])
        Batch = self.env['ksw.pay.batch']
        self.env['ksw.client.multiplier.rate'].search(
            [('client_id', '=', self.client.id)]).multiplier = 9.0
        data = Batch._bas_fetch_orood(date(2026, 9, 1), date(2026, 9, 6))['cm driver 1']
        self.assertAlmostEqual(data['mult'], 4.0)

    # -- back-dated changes ---------------------------------------------------
    @freeze_time('2028-08-20')
    def test_a_draft_month_is_corrected_in_place(self):
        self._bas([(CODE, date(2028, 7, 5), 10), (CODE, date(2028, 7, 25), 10)])
        entry = self._imported_entry()
        self.assertAlmostEqual(entry.quantity, 40.0)
        req = self._approve_change(1.5, date(2028, 7, 20))
        # 10 × 2.0 before the 20th, 10 × 1.5 from it.
        self.assertAlmostEqual(entry.quantity, 35.0)
        self.assertEqual(entry.state, 'draft')
        self.assertIn('corrected in place', ''.join(req.message_ids.mapped('body')))

    @freeze_time('2028-08-20')
    def test_an_approved_unpaid_month_keeps_its_approval(self):
        self._bas([(CODE, date(2028, 7, 5), 30)])
        entry = self._imported_entry()
        run = entry.batch_id.run_id
        entry.sudo().write({'state': 'approved'})
        entry.batch_id.sudo().with_context(ksw_entry_sync=True).write({'state': 'approved'})
        self.env['ksw.pay.run.line'].sudo().create({
            'run_id': run.id, 'employee_id': self.driver.id, 'earnings': entry.amount})
        run.sudo().write({'state': 'approved'})
        before = entry.amount
        with patch.object(type(run), '_resync_vacation_line', autospec=True) as resync:
            self._approve_change(1.0, date(2028, 7, 1))
        self.assertAlmostEqual(entry.quantity, 30.0)
        self.assertEqual(entry.state, 'approved')
        self.assertLess(entry.amount, before)
        resync.assert_called_once()
        self.assertEqual(resync.call_args.args[0], run)
        self.assertEqual(resync.call_args.args[1], self.driver)

    @freeze_time('2028-08-20')
    def test_a_paid_month_stays_and_the_difference_goes_to_the_next(self):
        self._bas([(CODE, date(2028, 7, 5), 30)])
        entry = self._imported_entry()
        run = entry.batch_id.run_id
        self.env['ksw.pay.run.line'].sudo().create({
            'run_id': run.id, 'employee_id': self.driver.id, 'earnings': entry.amount})
        run.sudo().write({'state': 'paid'})
        paid_qty, paid_amount = entry.quantity, entry.amount
        _rate, new_amount = self.c_trips._resolve(
            self.driver, quantity=45.0, threshold=entry.threshold_qty,
            date=entry.period, band_scale=entry._band_scale())
        self.assertNotAlmostEqual(new_amount, paid_amount)

        self._approve_change(1.5, date(2028, 7, 1))

        self.assertEqual((entry.quantity, entry.amount), (paid_qty, paid_amount))
        carried = self.env['ksw.pay.entry'].search([
            ('component_id', '=', self.c_remainder.id),
            ('employee_id', '=', self.driver.id)])
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried.period, AUG)
        self.assertEqual(carried.state, 'draft')
        self.assertAlmostEqual(carried.amount, round(new_amount - paid_amount, 2))
        self.assertLess(carried.amount, 0)

    @freeze_time('2028-08-20')
    def test_a_change_that_ends_before_the_loads_touches_nothing(self):
        self._bas([(CODE, date(2028, 7, 25), 10)])
        entry = self._imported_entry()
        self.env['ksw.client.multiplier.rate'].create({
            'client_id': self.client.id, 'multiplier': 2.0,
            'date_from': date(2028, 7, 10)})
        self._approve_change(1.0, date(2028, 7, 1))
        # Applies 1–9 July only; the loads are on the 25th.
        self.assertAlmostEqual(entry.quantity, 20.0)
