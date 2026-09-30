"""What a vacation request latched from the Commissions app.

One row per ``ksw.pay.entry`` the request saw when it was latched (see
``hr.leave._latch_commission_entries``). The figures are copied, not
related: the point of the row is to keep saying what Accounting reviewed
after the entry, its batch or its month has moved on.
"""
from odoo import api, fields, models

from .ksw_pay_batch import BATCH_STATES


class KswLeaveCommissionEntry(models.Model):
    _name = 'ksw.leave.commission.entry'
    _description = 'Commission Entry Latched on a Vacation Request'
    _order = 'leave_id, included desc, period, id'

    leave_id = fields.Many2one(
        'hr.leave', required=True, ondelete='cascade', index=True)
    entry_id = fields.Many2one(
        'ksw.pay.entry', ondelete='set null', index=True,
        help='The entry this row was copied from. Empty if it has since been '
             'deleted — the row still says what was latched.')
    included = fields.Boolean(
        help='Paid on the vacation payslip. Unticked: it was recorded but '
             'awaiting General Manager approval when latched.')
    period = fields.Date()
    pay_type = fields.Char()
    date = fields.Date()
    batch_id = fields.Many2one('ksw.pay.batch', ondelete='set null')
    batch_name = fields.Char()
    batch_state = fields.Selection(
        BATCH_STATES, string='Status When Latched')
    amount = fields.Float(digits=(16, 2))

    @api.model
    def _row_from_entry(self, entry):
        """The display row for a live entry (same keys as :meth:`_as_row`)."""
        what = entry.component_id.name or ''
        if entry.option_id:
            what = '%s — %s' % (what, entry.option_id.name)
        batch = entry.batch_id
        return {
            'period': entry.period,
            'what': what,
            'date': entry.date,
            'batch': batch,
            'batch_name': batch.name,
            # The row's own status: with sub-batches, a row can be approved
            # while its batch is still open.
            'state_key': entry.state,
            'state': self._state_label(entry.state),
            'amount': entry.amount,
        }

    @api.model
    def _state_label(self, key):
        # Translated when shown, not when latched: the row is read in
        # whichever language the viewer uses.
        return dict(self._fields['batch_state']._description_selection(
            self.env)).get(key, '')

    @api.model
    def _vals_from_entry(self, leave, entry, included):
        row = self._row_from_entry(entry)
        return {
            'leave_id': leave.id,
            'entry_id': entry.id,
            'included': included,
            'period': row['period'],
            'pay_type': row['what'],
            'date': row['date'],
            'batch_id': row['batch'].id,
            'batch_name': row['batch_name'],
            'batch_state': row['state_key'],
            'amount': row['amount'],
        }

    def _as_row(self):
        self.ensure_one()
        return {
            'period': self.period,
            'what': self.pay_type or '',
            'date': self.date,
            'batch': self.batch_id,
            'batch_name': self.batch_name,
            'state': self._state_label(self.batch_state),
            'amount': self.amount,
        }
