# -*- coding: utf-8 -*-
"""The commissions override of ``sync_from_bas`` must track KSW_ext_sync's.

KSW_ext_sync's BAS sync gained a time budget (Sep 2026, after the cron was
found reloading production every four minutes — see
`KSW_ext_sync/tests/test_bas_upsert.py`). `sync_from_bas` now takes
`deadline=` / `commit=` and returns whether the pass completed, so the
orchestrator knows not to advance its watermark.

This module overrides that method to recompute effective reps. The override
kept the old signature, and the first run of the new orchestrator died on
`TypeError: KswBasCustomer.sync_from_bas() got an unexpected keyword
argument 'deadline'` — the whole sync, not just the customer step. Nothing
in either module's tests covered the seam.
"""
import inspect
from unittest.mock import patch

from odoo.addons.KSW_ext_sync.models.bas_customer import BASCustomer
from odoo.tests.common import TransactionCase


class TestBasSyncOverride(TransactionCase):

    def test_override_accepts_the_budget_arguments(self):
        sig = inspect.signature(type(self.env['ksw.bas.customer']).sync_from_bas)
        self.assertIn('deadline', sig.parameters)
        self.assertIn('commit', sig.parameters)

    def test_override_passes_the_budget_through_to_super(self):
        Customer = self.env['ksw.bas.customer']
        with patch.object(BASCustomer, 'sync_from_bas',
                          return_value=True) as parent:
            Customer.sync_from_bas(deadline=1234.5, commit=False)
        self.assertTrue(parent.called)
        self.assertEqual(parent.call_args.kwargs.get('deadline'), 1234.5)
        self.assertEqual(parent.call_args.kwargs.get('commit'), False)

    def test_override_returns_whether_the_pass_completed(self):
        """The orchestrator reads this to decide about the watermark."""
        Customer = self.env['ksw.bas.customer']
        for parent_result in (True, False):
            with patch.object(BASCustomer, 'sync_from_bas',
                              return_value=parent_result):
                self.assertEqual(
                    Customer.sync_from_bas(commit=False), parent_result)

    def test_effective_reps_still_recomputed(self):
        Customer = self.env['ksw.bas.customer']
        with patch.object(BASCustomer, 'sync_from_bas', return_value=True), \
                patch.object(type(Customer), '_recompute_effective_reps') as rec:
            Customer.sync_from_bas(commit=False)
        self.assertTrue(rec.called)
