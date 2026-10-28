import json
import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tools.misc import file_path

from ..models.res_partner import PIN_MAX_ACCURACY_M

_logger = logging.getLogger(__name__)

DRIVER_GROUP = 'KSW_water_delivery.group_water_driver'

# Bumped whenever the shell or the cached asset list changes. The service
# worker keys its cache on it, so a bump is how a driver's phone picks up a new
# version of the app -- there is no other route in, because the whole point is
# that it does not go to the network to find out.
APP_VERSION = '1.0.10'


class KswWaterApp(http.Controller):
    """The driver's app, served outside the Odoo web client on purpose.

    A lot of the customer sites have no mobile signal at all, so the note has
    to be captured at the client and sent when the truck is back in coverage.
    That rules out running this inside the web client: core's service worker
    (`addons/web/static/src/service_worker.js`) holds the session info it needs
    to replay a cached page in a plain module-level variable, and the browser
    kills an idle worker within seconds -- so a cold offline launch falls
    through to `/odoo/offline` rather than to the app. There is no queue, no
    local store and nothing to extend; Point of Sale has all of that and it is
    a bespoke client too.

    So this is a small static shell at its own URL with its own service worker
    and its own scope. It boots with no network at all, reads the clients out
    of IndexedDB, and posts the queue back through `/water/sync` when there is
    a connection. The Odoo app stays exactly as it is for dispatchers, billing
    and anyone looking at a finished note.

    Since Oct 2026 it is also the driver's ONLY app: the web-client New
    Delivery screen opens this instead. With signal a capture is sent the
    moment it is saved (`sent_live`), so "online" is simply a queue that
    empties at once -- one path, one set of rules, and an out-of-range note is
    held for the supervisor whether or not there was signal.
    """

    # ------------------------------------------------------------------
    # The shell
    # ------------------------------------------------------------------
    @http.route(['/water', '/water/'], type='http', auth='user', website=False)
    def water_app(self, **kwargs):
        """Deliberately renders no user data.

        Everything about this page is the same for every driver, which is what
        makes it safe to cache: what the phone stores of *this* driver goes to
        IndexedDB through `/water/reference`, where signing out can clear it.
        """
        return request.render('KSW_water_delivery.water_app_shell', {
            'app_version': APP_VERSION,
        })

    @http.route('/water/manifest.webmanifest', type='http', auth='public')
    def water_manifest(self, **kwargs):
        manifest = {
            'name': 'KSW Water Delivery',
            'short_name': 'Water',
            'description': 'Issue water delivery notes at the client.',
            'start_url': '/water/',
            'scope': '/water/',
            'display': 'standalone',
            'background_color': '#ffffff',
            'theme_color': '#0b5ed7',
            'orientation': 'portrait',
            'icons': [{
                'src': '/KSW_water_delivery/static/description/icon.svg',
                'sizes': 'any',
                'type': 'image/svg+xml',
                'purpose': 'any',
            }],
        }
        return request.make_response(
            json.dumps(manifest),
            headers=[('Content-Type', 'application/manifest+json'),
                     ('Cache-Control', 'no-cache')],
        )

    @http.route('/water/sw.js', type='http', auth='public')
    def water_service_worker(self, **kwargs):
        """Served from `/water/` and not from `/module/static/...` because a
        worker's default scope is its own directory: a script under
        `/KSW_water_delivery/static/` could never control `/water/`."""
        try:
            path = file_path('KSW_water_delivery/static/src/app/sw.js')
        except (FileNotFoundError, ValueError):
            return request.not_found()
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        source = source.replace('__APP_VERSION__', APP_VERSION)
        return request.make_response(source, headers=[
            ('Content-Type', 'text/javascript; charset=utf-8'),
            # Without this the browser refuses the registration: a worker at
            # /water/sw.js may only claim /water/ unless the server widens it.
            ('Service-Worker-Allowed', '/water/'),
            ('Cache-Control', 'no-cache'),
        ])

    # ------------------------------------------------------------------
    # Data the phone has to be holding before it loses signal
    # ------------------------------------------------------------------
    def _check_driver(self):
        user = request.env.user
        if not user.has_group(DRIVER_GROUP):
            raise AccessError(_('Only drivers can issue water delivery notes.'))
        employee = user.sudo().employee_id
        if not employee:
            raise UserError(_(
                'Your user account is not linked to an employee record, so the '
                'delivery note cannot record who delivered it. Ask HR to link them.'
            ))
        return employee

    @http.route('/water/reference', type='jsonrpc', auth='user')
    def water_reference(self, **kwargs):
        """Everything the app needs to work with no connection at all.

        Small on purpose -- the pilot branch serves around 60 clients and 257
        of 312 buy exactly one product -- so it is refreshed in full every time
        the phone has a connection rather than diffed. A stale snapshot is the
        real risk here, not the bytes.
        """
        employee = self._check_driver()
        env = request.env
        Picking = env['stock.picking'].sudo()
        Pricing = env['ksw.water.pricing']

        # Fetched from the fleet assignment, never offered as a choice.
        vehicle, vehicle_problem = Picking._water_assigned_vehicle(employee)
        picking_type = Picking._water_picking_type_for(vehicle)
        normal, rated = Picking._water_client_scope(employee, picking_type)

        by_partner = {}
        for partner_id, entries in Pricing._partner_products(rated).items():
            by_partner[partner_id] = [{
                'id': product.id,
                'name': product.display_name,
                'uom': product.uom_id.name,
                # Display only. The note is priced from the client's
                # pricelist at the moment it is issued, never from what the
                # phone is holding -- see `stock.picking._issue_water_note`.
                'price': price,
            } for product, price in entries]

        normal_ids = set(normal.ids)
        # So the phone can say "sent, waiting for the office" instead of
        # offering the button again to the next driver at the same gate.
        open_dispatches = env['ksw.water.dispatch'].sudo()._water_open_for_driver(employee)
        pending_ids = env['ksw.water.location.request'].sudo()._pending_partner_ids(
            (rated | open_dispatches.partner_id).ids)
        anywhere = env['stock.picking.type']._water_cash_partner_ids()
        clients = [{
            'id': partner.id,
            'name': partner.display_name,
            'lat': partner.partner_latitude or 0.0,
            'lon': partner.partner_longitude or 0.0,
            # A branch cash customer stands for whoever walked in: no site.
            'anywhere': partner.id in anywhere,
            'usual': partner.id in normal_ids,
            'location_pending': partner.id in pending_ids,
            'products': by_partner.get(partner.id, []),
        } for partner in rated.sudo() if by_partner.get(partner.id)]
        clients.sort(key=lambda c: (not c['usual'], c['name']))

        # Open dispatches, cached with the rest so the driver can confirm one
        # at a site with no signal. Listed whatever the branch's mode: a
        # dispatch that exists is work he has been given.
        dispatches = []
        for dispatch in open_dispatches:
            partner, product = dispatch.partner_id, dispatch.product_id
            rate = Pricing._rate_for(partner, product, dispatch.quantity_m3 or 1.0)
            dispatches.append({
                'id': dispatch.id,
                'name': dispatch.name,
                'client': {
                    'id': partner.id,
                    'name': partner.display_name,
                    'lat': partner.partner_latitude or 0.0,
                    'lon': partner.partner_longitude or 0.0,
                    'usual': True,
                    'anywhere': partner.id in anywhere,
                    'location_pending': partner.id in pending_ids,
                    'products': [],
                },
                # Paid at the desk: the driver must not collect it again.
                'cash_paid': bool(dispatch.x_is_cash and dispatch.x_cash_received),
                'buyer': ' / '.join(filter(None, [dispatch.x_buyer_name, dispatch.x_buyer_phone])),
                'product': {
                    'id': product.id,
                    'name': product.display_name,
                    'uom': product.uom_id.name,
                    'price': rate.fixed_price if rate else 0.0,
                },
                'qty': dispatch.entered_qty,
                'uom': dispatch.entered_uom,
                'qty_m3': dispatch.quantity_m3,
                'vehicle_id': dispatch.vehicle_id.id,
                'note': dispatch.note or '',
                'dispatched_on': dispatch.dispatched_on.isoformat()
                if dispatch.dispatched_on else '',
            })

        return {
            'app_version': APP_VERSION,
            'driver': {'id': employee.id, 'name': employee.name},
            'user': {'id': request.env.user.id, 'name': request.env.user.name},
            'vehicle': {
                'id': vehicle.id,
                'name': vehicle.display_name,
                'capacity_m3': vehicle.x_capacity_m3 or 0.0,
            } if vehicle else None,
            # Shown instead of the form: with no (or an ambiguous) tanker the
            # phone has nothing to issue on, and only dispatch can fix it.
            'vehicle_problem': vehicle_problem or '',
            'rules': {
                'branch': picking_type.x_branch_code or '',
                'signature_required': bool(picking_type.x_signature_required),
                'location_rule': picking_type.x_location_rule or 'capture',
                'radius_m': picking_type.x_location_radius_m or 300,
                'pin_max_accuracy_m': PIN_MAX_ACCURACY_M,
                # 'dispatch': the app shows My Dispatches and no free form.
                'issue_mode': picking_type.x_issue_mode or 'driver',
            },
            'clients': clients,
            'dispatches': dispatches,
            'server_time': request.env['ksw.water.capture'].sudo()._now_iso(),
        }

    # ------------------------------------------------------------------
    # The queue coming back
    # ------------------------------------------------------------------
    @http.route('/water/sync', type='jsonrpc', auth='user')
    def water_sync(self, captures=None, **kwargs):
        """Take the phone's queue and answer for every item in it.

        Answers per uuid rather than for the batch: one capture failing because
        accounting never added a rate must not hold back the four behind it,
        and the phone needs to know which ones it may now drop.

        A capture is never rejected here for being late or for being far from
        the client. By the time this runs the water is in the customer's tank
        and the truck is at the depot -- refusing would produce a delivery with
        no document, which is the failure the whole module exists to remove.
        `_issue_water_note(..., location_mode='hold')` holds it for a
        dispatcher instead.
        """
        employee = self._check_driver()
        Capture = request.env['ksw.water.capture'].sudo()
        results = []
        for payload in (captures or []):
            uuid = (payload or {}).get('uuid')
            try:
                # Each capture in its own savepoint: a failure inside the ORM
                # would otherwise poison the transaction for the rest of the
                # batch, and the phone would keep resending all of them.
                with request.env.cr.savepoint():
                    capture = Capture._receive(payload, employee)
                results.append({
                    'uuid': uuid,
                    'status': capture.state,
                    'note': capture.picking_id.name or '',
                    'message': capture.hold_reason or capture.failure_reason or '',
                    # The phone drops a capture as soon as the server owns it.
                    # 'failed' counts: the record exists here, a dispatcher can
                    # see it and retry it, and keeping a copy on the phone that
                    # nothing will ever retry is how duplicates get made.
                    'settled': True,
                })
            except Exception as error:  # noqa: BLE001 - one bad item, not the batch
                _logger.exception('KSW_water_delivery: capture %s could not be received', uuid)
                results.append({
                    'uuid': uuid,
                    'status': 'error',
                    'message': str(error),
                    # NOT settled: nothing was recorded, so the phone must keep
                    # it and try again.
                    'settled': False,
                })
        return {'results': results}

    @http.route('/water/pin', type='jsonrpc', auth='user')
    def water_pin(self, pins=None, **kwargs):
        """Client locations the driver sent from the site. Each one becomes a
        request a dispatcher approves; none writes the client's location."""
        employee = self._check_driver()
        return {'results': request.env['res.partner'].sudo()._water_receive_pins(
            pins or [], employee)}

    @http.route('/water/recent', type='jsonrpc', auth='user')
    def water_recent(self, limit=20, **kwargs):
        """The driver's last few notes, so the app can show him what it sent."""
        employee = self._check_driver()
        pickings = request.env['stock.picking'].sudo().search([
            ('x_is_water_delivery', '=', True),
            ('x_driver_id', '=', employee.id),
        ], limit=min(int(limit), 50), order='id desc')
        return [{
            'id': picking.id,
            'name': picking.name,
            'client': picking.partner_id.display_name,
            'date': picking.x_captured_at and picking.x_captured_at.isoformat()
            or (picking.create_date and picking.create_date.isoformat()) or '',
            'qty': picking.x_load_qty,
            'total': picking.x_amount_total,
            'offline': picking.x_issued_offline,
            'held': picking.x_location_exception,
            'state': picking.state,
        } for picking in pickings]
