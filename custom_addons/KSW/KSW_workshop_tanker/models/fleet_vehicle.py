import logging
import re
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# How BAS names our trucks in vou10.COST_CENTER, against the fleet's own names:
# «ايسوزو338» / «ايسوزو 367» is IS338 / IS367, «T118» is T118. Anything else
# there (ice trucks, branches, private cars, "Customers car 1001") is not a
# fleet vehicle and is skipped.
_ISUZU_RE = re.compile(r'ايسوز')
_TRAILER_RE = re.compile(r'^t\s*\d+$', re.IGNORECASE)
# The fleet's own names, exactly (all 314 of KSWCO's trucks, 2026-10-13). Not
# "any trailing number": a stray "TEST-120" would make T120 ambiguous.
_FLEET_NAME_RE = {
    'isuzu': re.compile(r'^IS\s*(\d+)$', re.IGNORECASE),
    'trailer': re.compile(r'^T\s*(\d+)$', re.IGNORECASE),
}
_NEVER = re.compile(r'(?!)')
# BAS's stand-in driver on walk-in sales, not a person.
_NOT_A_DRIVER = 'customer car'
# The window the "current" driver is judged over. A truck changes hands rarely
# but sees one-day substitutes often (65 of 201 trucks in two weeks, nearly all
# of them a single load), so the driver is the one with the most loads in the
# window, never simply the last one.
DEFAULT_DAYS = 14


class KswFleetVehicle(models.Model):
    _inherit = 'ksw.fleet.vehicle'

    # Read-only here: written only by action_update_drivers_from_bas. Kept apart
    # from driver_id, which is dispatch's decision in Water Delivery > Tankers
    # (and checked there against the dispatcher's team); BAS records who
    # actually drove, which is a different question with its own answer.
    x_bas_driver_id = fields.Many2one(
        'hr.employee', string='Driver (BAS)', readonly=True,
        help='Who drove this truck most in BAS over the last two weeks. '
             'Refreshed by "Update Drivers from BAS".')
    x_bas_driver_label = fields.Char(
        string='BAS Driver Cost Centre', readonly=True,
        help='The driver as BAS names him («مركز تكلفة الموظف»). When Driver '
             '(BAS) is empty this is who BAS meant and Odoo could not place: '
             'set it as that employee\'s BAS Driver Cost Center.')
    x_bas_cost_centre = fields.Char(string='BAS Vehicle Cost Centre', readonly=True)
    x_bas_driver_date = fields.Date(string='Last Seen in BAS', readonly=True)
    x_attached_driver_id = fields.Many2one(
        'hr.employee', string='Attached Driver', compute='_compute_attached_driver_id',
        help='The driver BAS puts on this truck, or the default driver when '
             'BAS has none.')

    # "May the current user pick this truck on a workshop request". No groups=
    # and a search method, for the same reason as x_in_my_tanker_groups: a rule
    # on ksw.fleet.vehicle would hide trucks from every other screen too.
    x_workshop_in_scope = fields.Boolean(
        string='My Workshop Vehicle', compute='_compute_workshop_in_scope',
        search='_search_workshop_in_scope')

    @api.depends('x_bas_driver_id', 'driver_id')
    def _compute_attached_driver_id(self):
        for vehicle in self:
            vehicle.x_attached_driver_id = vehicle.x_bas_driver_id or vehicle.driver_id

    # ------------------------------------------------------------------
    # Workshop scope
    # ------------------------------------------------------------------
    def _workshop_scope_vehicle_ids(self):
        """None = no narrowing; otherwise the own-fleet truck ids allowed.

        A supervisor answers for the trucks in his tanker groups and for the
        trucks his people drive (his whole reporting line, himself included).
        Someone with neither — and the workshop itself — keeps the full list:
        narrowing him to nothing would stop him raising any request at all.
        """
        user = self.env.user
        if self.env.su or user.has_group('base.group_system') \
                or user.has_group('KSW_workshop.group_workshop_manager'):
            return None
        employee = user.sudo().employee_id
        if not employee:
            return None
        allowed = set(employee.x_water_tanker_group_ids.filtered('active').vehicle_ids.ids)
        team = self.env['hr.employee'].sudo().search([('id', 'child_of', employee.id)])
        allowed |= set(self.sudo().search([
            '|', ('x_bas_driver_id', 'in', team.ids),
            '&', ('x_bas_driver_id', '=', False), ('driver_id', 'in', team.ids),
        ]).ids)
        return allowed or None

    @api.depends_context('uid')
    def _compute_workshop_in_scope(self):
        allowed = self._workshop_scope_vehicle_ids()
        own = set(self._own_partner_ids())
        for vehicle in self:
            vehicle.x_workshop_in_scope = (
                allowed is None or vehicle.client_id.id not in own or vehicle.id in allowed)

    def _search_workshop_in_scope(self, operator, value):
        # Odoo 19 hands a boolean condition over as in/not in (gotcha #24).
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        allowed = self._workshop_scope_vehicle_ids()
        if allowed is None:
            return [] if wanted else [('id', '=', False)]
        # A client's vehicle is the client's, not a supervisor's: never narrowed.
        own = self._own_partner_ids()
        if wanted:
            return ['|', ('client_id', 'not in', own), ('id', 'in', list(allowed))]
        return [('client_id', 'in', own), ('id', 'not in', list(allowed))]

    # ------------------------------------------------------------------
    # BAS
    # ------------------------------------------------------------------
    @staticmethod
    def _bas_vehicle_key(label):
        """('isuzu' | 'trailer', number) for a BAS vehicle cost centre, else None."""
        text = ' '.join((label or '').replace('\xa0', ' ').split())
        nums = re.findall(r'\d+', text)
        if not nums:
            return None
        number = nums[-1].lstrip('0')
        if _ISUZU_RE.search(text):
            return ('isuzu', number)
        if _TRAILER_RE.match(text):
            return ('trailer', number)
        return None

    def _bas_fetch_vehicle_drivers(self, date_from):
        """Loads per (vehicle cost centre, driver cost centre) since date_from."""
        ftypes = [c.strip() for c in (self.env['ir.config_parameter'].sudo().get_param(
            'ksw_commissions.orood_ftypes', '600') or '').split(',') if c.strip()]
        if not ftypes:
            return []
        conn = self.env['ksw.bas.connector']._bas_connect()
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(
                "SELECT RTRIM(h.COST_CENTER) AS cc, RTRIM(h.COST_CENTER2) AS driver_cc, "
                "       COUNT(*) AS loads, MAX(h.FDATE) AS last_date "
                "FROM vou10 h "
                "WHERE h.FTYPE IN (" + ','.join(['%s'] * len(ftypes)) + ") "
                "  AND h.FDATE >= %s "
                "  AND ISNULL(h.COST_CENTER, '') <> '' AND ISNULL(h.COST_CENTER2, '') <> '' "
                "GROUP BY RTRIM(h.COST_CENTER), RTRIM(h.COST_CENTER2)",
                tuple(ftypes) + (fields.Date.to_string(date_from),))
            return cur.fetchall()
        finally:
            conn.close()

    def _bas_driver_index(self):
        """(normalised BAS label -> employee, employee no. -> employee).

        The label HR keeps on the employee is the key (`x_bas_driver_cost_center`
        and its alternates — the same ones the commission pull trusts). The
        label's trailing number is the employee number, and is used only for a
        man with no label of his own: where HR set a label, HR decided.
        """
        norm = self.env['ksw.pay.batch']._norm_cc
        by_label, clashes = {}, set()
        by_number = defaultdict(list)
        employees = self.env['hr.employee'].sudo().search([])
        for employee in employees:
            labels = [employee.x_bas_driver_cost_center or '']
            labels += (employee.x_bas_driver_cost_center_alt or '').split(',')
            keys = {norm(v) for v in labels if norm(v)}
            for key in keys:
                if key in by_label and by_label[key] != employee:
                    clashes.add(key)
                by_label[key] = employee
            if not keys and employee.x_employee_no:
                by_number[employee.x_employee_no.strip().lstrip('0')].append(employee)
        for key in clashes:
            del by_label[key]
        return by_label, {n: e[0] for n, e in by_number.items() if len(e) == 1}

    def _bas_match_driver(self, label, by_label, by_number):
        norm = self.env['ksw.pay.batch']._norm_cc(label)
        if norm in by_label:
            return by_label[norm]
        nums = re.findall(r'\d+', norm)
        return by_number.get(nums[-1].lstrip('0')) if nums else None

    def action_update_drivers_from_bas(self):
        """Put on each of our trucks the driver BAS says drove it most lately."""
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only a system administrator can update the drivers from BAS.'))
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            'KSW_workshop_tanker.bas_driver_days', DEFAULT_DAYS) or DEFAULT_DAYS)
        rows = self._bas_fetch_vehicle_drivers(fields.Date.context_today(self) - timedelta(days=days))

        Vehicle = self.sudo()
        fleet = defaultdict(list)
        for vehicle in Vehicle.search([('x_is_own_vehicle', '=', True)]):
            match = _FLEET_NAME_RE.get(vehicle.vehicle_type, _NEVER).match(vehicle.name or '')
            if match:
                fleet[(vehicle.vehicle_type, match.group(1).lstrip('0'))].append(vehicle)

        # The dominant driver per truck: most loads, then the latest.
        best = {}
        for row in rows:
            key = self._bas_vehicle_key(row['cc'])
            driver = ' '.join((row['driver_cc'] or '').replace('\xa0', ' ').split())
            if not key or not driver or _NOT_A_DRIVER in driver.lower():
                continue
            rank = (row['loads'], row['last_date'])
            if key not in best or rank > best[key][0]:
                best[key] = (rank, row['cc'].replace('\xa0', ' ').strip(), driver)

        by_label, by_number = self._bas_driver_index()
        updated, unchanged = 0, 0
        not_in_fleet, unplaced = [], []
        for key, ((_loads, last_date), cc, driver_label) in sorted(best.items()):
            vehicles = fleet.get(key, [])
            if len(vehicles) != 1:
                # None: BAS has a truck the fleet does not. Two: a guess would
                # attach one truck's driver to another.
                not_in_fleet.append(cc)
                continue
            vehicle = vehicles[0]
            employee = self._bas_match_driver(driver_label, by_label, by_number)
            if not employee:
                unplaced.append(f'{vehicle.name}: {driver_label}')
            vals = {
                'x_bas_driver_id': employee.id if employee else False,
                'x_bas_driver_label': driver_label,
                'x_bas_cost_centre': cc,
                'x_bas_driver_date': last_date.date() if hasattr(last_date, 'date') else last_date,
            }
            if vehicle.x_bas_driver_id.id == vals['x_bas_driver_id'] \
                    and vehicle.x_bas_driver_label == driver_label:
                unchanged += 1
                vals = {'x_bas_driver_date': vals['x_bas_driver_date']}
            else:
                updated += 1
            vehicle.write(vals)

        _logger.info('Drivers from BAS: %s updated, %s unchanged, %s not in fleet, %s unplaced',
                     updated, unchanged, len(not_in_fleet), len(unplaced))
        message = _('%(updated)s trucks changed driver, %(unchanged)s unchanged (last %(days)s days of BAS sales).',
                    updated=updated, unchanged=unchanged, days=days)
        if unplaced:
            message += ' ' + _('%(n)s drivers BAS names are not linked to an employee '
                               '(set their BAS Driver Cost Center): %(list)s',
                               n=len(unplaced), list=', '.join(unplaced[:15]))
        if not_in_fleet:
            message += ' ' + _('%(n)s BAS trucks have no single match in the fleet: %(list)s',
                               n=len(not_in_fleet), list=', '.join(not_in_fleet[:15]))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Drivers updated from BAS'),
                'message': message,
                'type': 'warning' if unplaced or not_in_fleet else 'success',
                'sticky': bool(unplaced or not_in_fleet),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }
