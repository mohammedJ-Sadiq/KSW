import logging
from collections import defaultdict

from odoo import api, models

_logger = logging.getLogger(__name__)


class BASCustomer(models.Model):
    _inherit = 'ksw.bas.customer'

    @api.model
    def _apply_payment_terms(self):
        changed = super()._apply_payment_terms()
        self._apply_risk_grace()
        return changed

    # ------------------------------------------------------------------
    # BAS credit stop onto the contact's grace period
    # ------------------------------------------------------------------
    # BAS keeps two day counts per customer: INVDAYS, the payment term it
    # stamps on every invoice (invoice_term_days, already applied as the
    # contact's payment terms), and LIMT_DAYS, the larger credit stop
    # (credit_term_days, >= INVDAYS on 584 of 587 accounts). The grace after
    # the due date is the gap between them: 30 / 90 -> 60 grace days.
    @api.model
    def _apply_risk_grace(self):
        Partner = self.env['res.partner']
        # Same contact resolution as _apply_payment_terms: the linked contact
        # plus every contact carrying the account number.
        by_code = defaultdict(lambda: Partner)
        for code_field in ('x_client_account_number', 'x_bas_code'):
            if code_field not in Partner._fields:
                continue
            for partner in Partner.search([(code_field, '!=', False)]):
                by_code[(partner[code_field] or '').strip()] |= partner

        wanted = {}
        for rec in self.search([('credit_term_days', '>', 0)]):
            grace = max(rec.credit_term_days - rec.invoice_term_days, 0)
            for partner in rec.partner_id | by_code.get(rec.bas_code, Partner):
                partner = partner.commercial_partner_id
                # One customer on several BAS accounts: the most lenient stop
                # wins, so a second account never blocks him earlier than
                # BAS itself would on the first.
                wanted[partner] = max(wanted.get(partner, 0), grace)

        changed = 0
        for partner, grace in wanted.items():
            if partner.x_risk_grace_policy not in (False, 'bas'):
                continue  # somebody decided by hand
            if (partner.x_risk_grace_policy == 'bas'
                    and partner.x_risk_grace_days == grace):
                continue
            partner.with_context(ksw_bas_risk_grace=True).write({
                'x_risk_grace_policy': 'bas',
                'x_risk_grace_days': grace,
            })
            changed += 1
        if changed:
            _logger.info('KSW BAS: grace period set on %d customer(s)', changed)
        return changed
