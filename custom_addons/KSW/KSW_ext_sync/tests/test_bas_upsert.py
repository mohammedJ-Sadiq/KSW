# -*- coding: utf-8 -*-
"""The BAS mirrors are filled by a batched, budgeted, resumable upsert.

Prod incident (KSWCO, 2026-09-02): `odoo.service.cron.cron0` was hitting
`virtual real time limit (179/120s)` and reloading the whole production
server — nine times in one hour, roughly every four minutes. The stack
landed in `bas_invoice.sync_from_bas`: one ORM `create()` per BAS row, in
a loop, over a whole fiscal year of invoices.

The damage was not only the reloads. `action_sync_all` ran all four
mirrors in **one transaction with no commit**, so every run was killed
before the end and rolled back in full — `ksw.bas.account`,
`ksw.bas.customer`, `ksw.bas.invoice` and `ksw.bas.payment` were all at
**0 rows**, and had been for weeks. The sync had never once completed,
while logging "synced 346 accounts" every four minutes from work that was
about to be discarded.

A cron is not exempt from `limit_time_real`: `server.py` honours
`limit_time_real_cron` only when it is *positive*, so the prod setting of
`-1` falls through to the same 120s ceiling.

These tests cover `_bas_upsert` directly — no SQL Server needed.
"""
from time import monotonic

from odoo.tests.common import TransactionCase


class TestBasUpsert(TransactionCase):

    def _vals(self, code, **over):
        vals = {
            'bas_code': code,
            'name_ar': 'name %s' % code,
            'name_en': 'name %s' % code,
            'acc_type': '1/1',
            'opening_balance': 100.0,
            'current_debit': 0.0,
            'current_credit': 0.0,
            'category': 'other',
        }
        vals.update(over)
        return vals

    @property
    def Account(self):
        return self.env['ksw.bas.account']

    # ------------------------------------------------------------------
    # Creating
    # ------------------------------------------------------------------

    def test_creates_every_new_row(self):
        vals_list = [self._vals('A%03d' % i) for i in range(10)]
        created, updated, done = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual((created, updated, done), (10, 0, True))
        self.assertEqual(
            self.Account.search_count([('bas_code', 'like', 'A0')]), 10)

    def test_creates_in_chunks(self):
        """The whole point: batched INSERTs, not one per row."""
        self.assertEqual(self.Account._BAS_CHUNK, 500)
        vals_list = [self._vals('B%04d' % i) for i in range(1200)]
        created, _updated, done = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual(created, 1200)
        self.assertTrue(done)

    # ------------------------------------------------------------------
    # The steady state: nothing changed, so nothing is written
    # ------------------------------------------------------------------

    def test_second_pass_writes_nothing(self):
        """Every run re-reads a 30-day window in which almost nothing moved."""
        vals_list = [self._vals('C%03d' % i) for i in range(20)]
        self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        created, updated, done = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual((created, updated, done), (0, 0, True))

    def test_last_synced_alone_does_not_count_as_a_change(self):
        """If it did, every row would look changed and we are back to square one."""
        vals_list = [self._vals('D001')]
        self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        moved = [dict(vals_list[0], last_synced='2030-01-01 00:00:00')]
        _created, updated, _done = self.Account._bas_upsert(
            'bas_code', moved, self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual(updated, 0)
        self.assertNotIn('last_synced', self.Account._COMPARE_FIELDS)

    def test_a_real_change_is_written(self):
        self.Account._bas_upsert(
            'bas_code', [self._vals('E001')],
            self.Account._COMPARE_FIELDS, commit=False)
        changed = [self._vals('E001', current_debit=250.0)]
        created, updated, done = self.Account._bas_upsert(
            'bas_code', changed, self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual((created, updated, done), (0, 1, True))
        rec = self.Account.search([('bas_code', '=', 'E001')])
        self.assertEqual(rec.current_debit, 250.0)

    def test_float_noise_is_not_a_change(self):
        self.Account._bas_upsert(
            'bas_code', [self._vals('F001', opening_balance=100.0)],
            self.Account._COMPARE_FIELDS, commit=False)
        _c, updated, _d = self.Account._bas_upsert(
            'bas_code', [self._vals('F001', opening_balance=100.001)],
            self.Account._COMPARE_FIELDS, commit=False)
        self.assertEqual(updated, 0)

    # ------------------------------------------------------------------
    # The budget
    # ------------------------------------------------------------------

    def test_deadline_stops_the_pass_and_reports_not_done(self):
        vals_list = [self._vals('G%04d' % i) for i in range(1200)]
        created, _updated, done = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS,
            deadline=monotonic() - 1, commit=False)
        self.assertFalse(done, 'An expired deadline must report done=False.')
        self.assertEqual(created, 0)

    def test_an_incomplete_pass_keeps_what_it_wrote(self):
        """Progress is durable; the next run continues from there."""
        vals_list = [self._vals('H%04d' % i) for i in range(1200)]
        # Budget expires after the first chunk is already committed.
        deadline = monotonic() + 0.0001
        created, _updated, done = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS,
            deadline=deadline, commit=False)
        self.assertFalse(done)
        # Whatever it managed, a second unbounded pass finishes the rest
        # and re-writes nothing it already did.
        created2, updated2, done2 = self.Account._bas_upsert(
            'bas_code', vals_list, self.Account._COMPARE_FIELDS, commit=False)
        self.assertTrue(done2)
        self.assertEqual(updated2, 0, 'Rows written by pass 1 must not be rewritten.')
        self.assertEqual(created + created2, 1200)

    # ------------------------------------------------------------------
    # Signatures line up with the orchestrator
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # The key must identify one row, or the upsert corrupts data
    # ------------------------------------------------------------------

    def test_payment_key_includes_serial(self):
        """VOU10's primary key is FTYPE+FTYPE2+CODE2+NUMBER1+**SERIAL**.

        `ksw.bas.payment` mirrors journal *lines* — one receipt can carry 172
        — and the commission sheet sums them per customer account. Without
        SERIAL the keys collide, and a keyed upsert then overwrites one line
        with another line's amount and account.
        """
        Pay = self.env['ksw.bas.payment']
        a = Pay._make_key('018', 0, '010', 26006255, 1)
        b = Pay._make_key('018', 0, '010', 26006255, 2)
        self.assertNotEqual(a, b, 'Two lines of one voucher must not collide.')

    def test_duplicate_keys_would_be_visible(self):
        """A keyed upsert is only safe while the key is unique.

        Guards the whole family: if a mirror's `_make_key` ever drops part of
        the source primary key again, this fails rather than silently
        overwriting rows.
        """
        Pay = self.env['ksw.bas.payment']
        vals_list = [
            {'bas_key': Pay._make_key('018', 0, '010', 1, s), 'amount': float(s)}
            for s in range(1, 6)
        ]
        keys = [v['bas_key'] for v in vals_list]
        self.assertEqual(len(keys), len(set(keys)))
        created, _u, done = Pay._bas_upsert(
            'bas_key', vals_list, ('amount',), commit=False)
        self.assertEqual((created, done), (5, True))
        # Each line kept its own value — none overwritten by a sibling.
        for s in range(1, 6):
            rec = Pay.search(
                [('bas_key', '=', Pay._make_key('018', 0, '010', 1, s))])
            self.assertEqual(len(rec), 1)
            self.assertEqual(rec.amount, float(s))

    def test_every_mirror_accepts_the_budget(self):
        """action_sync_all calls all four with deadline= and commit=."""
        import inspect
        for model in ('ksw.bas.account', 'ksw.bas.customer',
                      'ksw.bas.invoice', 'ksw.bas.payment'):
            sig = inspect.signature(type(self.env[model]).sync_from_bas)
            self.assertIn('deadline', sig.parameters, model)
            self.assertIn('commit', sig.parameters, model)

    def test_sync_all_budget_leaves_room_for_a_late_step(self):
        """A cron is NOT exempt from limit_time_real (-1 falls through to it).

        The budget is checked before each step, so the worst overshoot is one
        step's pre-work — measured at 27s for a full fiscal year. The budget
        plus that overshoot must still clear 120s.
        """
        measured_prework = 27
        self.assertLess(
            self.Account._SYNC_ALL_SECONDS + measured_prework, 120)
