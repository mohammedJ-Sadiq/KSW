import logging
import re
from collections import defaultdict

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# BAS cost-centre groups (sub-plans of "Cost Centres") that hold one account
# per vehicle, named with the vehicle's number: «ايسوزو 377», «T95», «تيدر رقم 80».
_PLAN_KIND = {
    'الايسوزو': 'isuzu',
    'التريلات T': 'trailer',
    'مركز تكلفة التيادرT': 'tedder',
    'سيارات خصوصي': 'private',
}
# Which BAS groups a fleet vehicle type may match. Fleet trailers are named
# like BAS's trailer group (T120 = «T120»); tankers «تيدر رقم 120» are other
# vehicles, which the fleet keeps as type 'other' under a bare number.
_KINDS_BY_TYPE = {
    'isuzu': ('isuzu',),
    'trailer': ('trailer',),
    'other': ('tedder', 'private'),
}


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    x_analytic_account_id = fields.Many2one(
        'account.analytic.account', string='Cost Centre',
        help="BAS cost centre of this vehicle. Spare parts issued to it in the "
             "workshop are charged here.")

    @staticmethod
    def _ksw_number(text):
        nums = re.findall(r'\d+', text or '')
        return nums[-1].lstrip('0') if nums else ''

    @api.model
    def _ksw_match_cost_centres(self):
        """Link each vehicle to the one BAS cost centre with its type and number.

        Leaves a vehicle alone when it already has a cost centre (Odoo is the
        master once set), when no account matches, or when more than one does:
        a guess would charge one truck's repairs to another.
        """
        root = self.env['account.analytic.plan'].search(
            [('name', '=', 'Cost Centres'), ('parent_id', '=', False)], limit=1)
        if not root:
            return {}
        index = defaultdict(list)
        for acc in self.env['account.analytic.account'].with_context(active_test=False).search(
                [('root_plan_id', '=', root.id)]):
            kind = _PLAN_KIND.get(acc.plan_id.name)
            num = self._ksw_number(acc.name)
            if kind and num:
                index[(kind, num)].append(acc)
        result = {'linked': 0, 'none': 0, 'ambiguous': 0}
        for vehicle in self.with_context(active_test=False).search([('x_analytic_account_id', '=', False)]):
            num = self._ksw_number(vehicle.name)
            # Every group this type may belong to, together: a bare number such
            # as 12 can be a tanker and a private car at once, and then it is
            # not ours to pick.
            found = [acc for kind in _KINDS_BY_TYPE.get(vehicle.vehicle_type, ())
                     for acc in index.get((kind, num), [])]
            if len(found) == 1:
                vehicle.x_analytic_account_id = found[0]
                result['linked'] += 1
            else:
                result['ambiguous' if found else 'none'] += 1
        _logger.info('Vehicle cost centres: %s', result)
        return result
