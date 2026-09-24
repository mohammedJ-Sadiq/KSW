import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# How far apart the phone's clock and the server's may be before it is worth
# saying so on the record. Offline capture moves the timestamp under the
# driver's control -- a device clock is settable -- so the two are kept apart
# and the gap is visible rather than reconciled into one "date".
CLOCK_SKEW_TOLERANCE_S = 15 * 60


class KswWaterCapture(models.Model):
    """One delivery as the driver's phone recorded it, at the client.

    This is not the delivery note. It is the phone's assertion about a moment
    the server did not witness, kept as its own record for three reasons that
    a field on the picking could not serve:

    * **Idempotency.** A queue that retries will eventually send the same
      capture twice -- a response lost on a flaky edge of coverage looks
      exactly like a failure. `uuid` is generated on the phone at capture and
      unique here, so the second attempt finds the first instead of issuing a
      second note for one load of water.
    * **The exception queue.** A capture whose location does not check out
      still produced a real delivery, so it has to exist somewhere a
      dispatcher can see it and decide. A refusal would just lose it.
    * **What was asserted, separately from what was issued.** The price the
      phone had cached, the position it recorded, the clock it recorded it by.
      When the note and the capture disagree, the disagreement is the finding.
    """
    _name = 'ksw.water.capture'
    _description = 'Water Delivery Captured Offline'
    _inherit = ['mail.thread']
    _order = 'captured_at desc, id desc'

    # Generated on the phone, at the moment of capture, before anything is
    # sent. This is the whole of the duplicate protection.
    uuid = fields.Char(string='Capture Reference', required=True, index=True,
                       readonly=True, copy=False)

    state = fields.Selection(
        [('pending', 'Received'),
         ('issued', 'Note Issued'),
         ('held', 'Waiting for Review'),
         ('rejected', 'Rejected'),
         ('failed', 'Could Not Be Issued')],
        default='pending', required=True, index=True, tracking=True,
    )

    # --- what the phone sent ---------------------------------------------
    driver_id = fields.Many2one('hr.employee', string='Driver', required=True,
                                index=True, readonly=True)
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Tanker', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Client', required=True,
                                 index=True, readonly=True)
    product_id = fields.Many2one('product.product', string='Product',
                                 required=True, readonly=True)
    entered_qty = fields.Float(string='Quantity', digits='Product Unit', readonly=True)
    entered_uom = fields.Selection(
        [('product', 'Product unit'), ('trip', 'Trips')],
        string='Entered In', default='product', readonly=True,
    )
    signature = fields.Binary(string='Customer Signature', readonly=True, copy=False)
    signed_by = fields.Char(string='Signed By', readonly=True)
    signature_origin = fields.Selection(
        [('on_site', 'Signed at the customer'),
         ('depot', 'Signed at the depot')],
        string='Signature Captured', readonly=True,
    )
    note = fields.Char(string='Remark', readonly=True)
    off_route = fields.Boolean(string='Off the Usual Route', readonly=True)

    gps_latitude = fields.Float(string='Latitude', digits=(10, 7), readonly=True)
    gps_longitude = fields.Float(string='Longitude', digits=(10, 7), readonly=True)
    gps_accuracy = fields.Float(string='GPS Accuracy (m)', readonly=True)
    distance_m = fields.Float(string='Distance from Client (m)', readonly=True)

    # The price the phone had in its cached rate snapshot. Display-only, and
    # recorded ONLY so it can be compared: the note is always priced from the
    # live register at the moment it is issued. A phone carrying a three-day-old
    # snapshot must never be able to invoice a stale rate -- but if the customer
    # signed a screen showing one figure and the note says another, somebody has
    # to be told, and this is what tells them.
    cached_price = fields.Float(string='Price Shown on the Phone',
                                digits='Product Price', readonly=True)
    issued_price = fields.Float(string='Price on the Note',
                                digits='Product Price', readonly=True)
    price_changed = fields.Boolean(
        string='Rate Changed Since Capture', compute='_compute_price_changed',
        store=True, index=True,
    )

    # --- the three timestamps, deliberately not one ----------------------
    captured_at = fields.Datetime(
        string='Captured At', required=True, readonly=True, index=True,
        help="When the driver's phone says the delivery happened. The phone's "
             "own clock, which the driver can change.",
    )
    received_at = fields.Datetime(
        string='Received At', required=True, readonly=True,
        default=lambda self: fields.Datetime.now(),
        help='When the server received it. Set here, not by the phone.',
    )
    queued_seconds = fields.Integer(
        string='Time in the Queue (s)', compute='_compute_queued_seconds', store=True,
        help='How long the capture sat on the phone before it reached a '
             'connection. A large gap is not wrong in itself -- it is what '
             'offline capture is for -- but it is what makes the position and '
             'the timestamp worth reading together.',
    )
    clock_skew_s = fields.Integer(
        string='Clock Skew (s)', readonly=True,
        help="Difference between the phone's clock and the server's at the "
             "moment the capture was sent. Large values mean the captured "
             "timestamp cannot be taken at face value.",
    )

    app_version = fields.Char(string='App Version', readonly=True)

    # --- the outcome ------------------------------------------------------
    picking_id = fields.Many2one('stock.picking', string='Delivery Note',
                                 readonly=True, copy=False)
    picking_state = fields.Selection(related='picking_id.state', string='Note Status')
    hold_reason = fields.Char(string='Why It Is Held', readonly=True, tracking=True)
    failure_reason = fields.Text(string='Why It Failed', readonly=True)
    reviewed_by = fields.Many2one('res.users', string='Reviewed By', readonly=True)
    reviewed_on = fields.Datetime(string='Reviewed On', readonly=True)
    review_note = fields.Char(string='Reviewer\'s Note')

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company,
                                 required=True)

    _uuid_uniq = models.Constraint(
        'unique(uuid)',
        'This delivery has already been received from the driver\'s phone.',
    )

    # ------------------------------------------------------------------
    @api.depends('captured_at', 'received_at')
    def _compute_queued_seconds(self):
        for capture in self:
            if capture.captured_at and capture.received_at:
                delta = capture.received_at - capture.captured_at
                capture.queued_seconds = max(0, int(delta.total_seconds()))
            else:
                capture.queued_seconds = 0

    @api.depends('cached_price', 'issued_price')
    def _compute_price_changed(self):
        for capture in self:
            capture.price_changed = bool(
                capture.cached_price and capture.issued_price
                and abs(capture.cached_price - capture.issued_price) > 0.0001
            )

    @api.depends('partner_id', 'captured_at')
    def _compute_display_name(self):
        for capture in self:
            capture.display_name = '%s — %s' % (
                capture.partner_id.display_name or _('Unknown client'),
                fields.Datetime.to_string(capture.captured_at) or '',
            )

    @api.model
    def _now_iso(self):
        """The server's clock, handed to the phone so it can show the driver
        how far its own has drifted before that turns into a captured time
        nobody can rely on."""
        return fields.Datetime.now().isoformat()

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------
    @api.model
    def _receive(self, payload, driver):
        """Record one capture from the phone and try to issue it.

        Idempotent on `uuid`, which is the point: the phone retries whenever it
        gets a connection, and a response lost on the way back is
        indistinguishable from a send that never arrived. Returning the
        existing capture is what stops one load of water becoming two notes.
        """
        uuid = (payload.get('uuid') or '').strip()
        if not uuid:
            raise UserError(_('The capture is missing its reference.'))

        existing = self.sudo().search([('uuid', '=', uuid)], limit=1)
        if existing:
            return existing

        captured_at = fields.Datetime.to_datetime(payload.get('captured_at'))
        now = fields.Datetime.now()
        if not captured_at:
            # A capture with no timestamp is still a real delivery; it just
            # cannot say when. The arrival time stands in.
            captured_at = now

        # Skew is measured against `sent_at` -- the phone's clock at the moment
        # it made this call -- and NOT against `captured_at`. The gap between
        # capture and arrival is the queue doing its job, which is what offline
        # capture is for; the two questions are "how long was it in his pocket"
        # (queued_seconds) and "can his clock be trusted at all" (this). Reading
        # the first as the second would flag every genuinely offline delivery.
        sent_at = fields.Datetime.to_datetime(payload.get('sent_at'))
        skew = int((sent_at - now).total_seconds()) if sent_at else 0

        values = {
            'uuid': uuid,
            'driver_id': driver.id,
            'vehicle_id': payload.get('vehicle_id') or False,
            'partner_id': payload.get('partner_id'),
            'product_id': payload.get('product_id'),
            'entered_qty': payload.get('entered_qty') or 0.0,
            'entered_uom': payload.get('entered_uom') or 'product',
            'signature': payload.get('signature') or False,
            'signed_by': payload.get('signed_by') or False,
            'signature_origin': payload.get('signature_origin') or False,
            'note': payload.get('note') or False,
            'off_route': bool(payload.get('off_route')),
            'gps_latitude': payload.get('gps_latitude') or 0.0,
            'gps_longitude': payload.get('gps_longitude') or 0.0,
            'gps_accuracy': payload.get('gps_accuracy') or 0.0,
            'cached_price': payload.get('cached_price') or 0.0,
            'captured_at': captured_at,
            'received_at': now,
            'clock_skew_s': skew,
            'app_version': payload.get('app_version') or False,
        }
        capture = self.sudo().create(values)
        capture._try_issue()
        return capture

    # ------------------------------------------------------------------
    # Issuing
    # ------------------------------------------------------------------
    def _payload(self):
        self.ensure_one()
        return {
            'partner_id': self.partner_id.id,
            'product_id': self.product_id.id,
            'entered_qty': self.entered_qty,
            'entered_uom': self.entered_uom,
            'driver_id': self.driver_id.id,
            'vehicle_id': self.vehicle_id.id,
            'signature': self.signature,
            'signed_by': self.signed_by,
            'signature_origin': self.signature_origin,
            'gps_latitude': self.gps_latitude,
            'gps_longitude': self.gps_longitude,
            'gps_accuracy': self.gps_accuracy,
            'note': self.note,
            'off_route': self.off_route,
            'issued_offline': True,
            'captured_at': self.captured_at,
        }

    def _try_issue(self):
        """Run the capture through the one issuance path.

        `location_mode='hold'`, and that is the whole difference between this
        and a live note: the water is already delivered, so a location the
        server cannot confirm makes the note something a dispatcher has to
        look at, never something that disappears.
        """
        Picking = self.env['stock.picking'].sudo()
        for capture in self:
            if capture.state in ('issued', 'rejected'):
                continue
            try:
                # A capture is the phone's word for what happened, but the
                # rules are the server's: the rate, the signature requirement
                # and the location rule are all read off live records inside
                # `_issue_water_note`, never off the payload.
                picking = Picking._issue_water_note(
                    capture._payload(), location_mode='hold')
            except UserError as error:
                # Not lost: a missing rate is a data problem somebody can fix,
                # and then this capture is retried. Losing the delivery because
                # accounting had not added a price is exactly the failure the
                # paper process already has.
                capture.write({
                    'state': 'failed',
                    'failure_reason': str(error),
                })
                _logger.warning(
                    'KSW_water_delivery: capture %s could not be issued: %s',
                    capture.uuid, error)
                continue
            except Exception as error:  # noqa: BLE001 - the queue must not stall
                capture.write({'state': 'failed', 'failure_reason': str(error)})
                _logger.exception(
                    'KSW_water_delivery: capture %s failed unexpectedly', capture.uuid)
                continue

            picking.x_capture_id = capture.id
            held = picking.x_location_exception
            capture.write({
                'picking_id': picking.id,
                'state': 'held' if held else 'issued',
                'hold_reason': picking.x_location_exception_reason or False,
                'failure_reason': False,
                'distance_m': picking.x_gps_distance_m,
                'issued_price': picking.sale_id.sudo().order_line[:1].price_unit,
            })
            if capture.price_changed:
                # The customer signed a screen showing the cached figure. The
                # note carries the live one, which is the right answer -- and
                # somebody still has to know the two differ.
                capture.message_post(body=Markup(
                    '<strong>%(title)s</strong><br/>'
                    '%(label_phone)s %(cached).2f · %(label_note)s %(issued).2f'
                ) % {
                    'title': _('The rate changed between capture and issue.'),
                    'label_phone': _('Shown on the phone:'),
                    'label_note': _('Priced on the note:'),
                    'cached': capture.cached_price,
                    'issued': capture.issued_price,
                })
            if abs(capture.clock_skew_s) > CLOCK_SKEW_TOLERANCE_S:
                capture.message_post(body=Markup('%(text)s') % {
                    'text': _(
                        "The phone's clock was %(minutes)d minutes away from "
                        "the server's, so the captured time cannot be taken at "
                        "face value.",
                        minutes=int(abs(capture.clock_skew_s) / 60),
                    ),
                })

    # ------------------------------------------------------------------
    # The dispatcher's two decisions
    # ------------------------------------------------------------------
    def _check_dispatcher(self):
        if self.env.su:
            return
        if not self.env.user.has_group('KSW_water_delivery.group_water_dispatcher'):
            raise UserError(_('Only a dispatcher may review offline deliveries.'))

    def action_accept(self):
        """Accept a held note: validate it, and only then write the signature.

        The ordering is the live path's ordering, not an exception to it --
        core's `_attach_sign()` renders the delivery slip at the moment the
        signature is written, so a signature applied before validation would
        attach a PDF showing quantities nobody had confirmed.
        """
        self._check_dispatcher()
        for capture in self:
            if capture.state != 'held':
                raise UserError(_('Only a delivery waiting for review can be accepted.'))
            picking = capture.picking_id.sudo()
            if not picking:
                raise UserError(_('This capture has no delivery note to accept.'))
            quantity = self.env['stock.picking']._water_quantity_in_product_uom(
                capture.vehicle_id, capture.entered_qty, capture.entered_uom)
            picking.move_ids.write({'quantity': quantity, 'picked': True})
            picking.with_context(skip_backorder=True).button_validate()
            self.env['stock.picking']._water_apply_signature(picking, capture._payload())
            # The flag is cleared because a human has now confirmed it; the
            # note keeps `x_issued_offline` and `x_captured_at` forever,
            # because those are what happened rather than a pending question.
            picking.write({
                'x_location_exception': False,
                'x_location_exception_reason': False,
            })
            picking.message_post(body=Markup(
                '<strong>%(title)s</strong> %(user)s<br/>%(reason)s'
            ) % {
                'title': _('Offline delivery accepted by'),
                'user': self.env.user.name,
                'reason': capture.hold_reason or '',
            })
            capture.write({
                'state': 'issued',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })

    def action_reject(self):
        """Reject a held note: cancel it.

        Only possible because a held note was never validated. This is the
        reason holding is done by withholding validation rather than by
        flagging a finished document -- there is no reversal to invent, and
        nothing was ever billable.
        """
        self._check_dispatcher()
        for capture in self:
            if capture.state != 'held':
                raise UserError(_('Only a delivery waiting for review can be rejected.'))
            if not capture.review_note:
                raise UserError(_(
                    'Say why this delivery is being rejected. The driver '
                    'recorded it as delivered, so the reason is the record.'
                ))
            picking = capture.picking_id.sudo()
            if picking:
                order = picking.sale_id
                picking.action_cancel()
                if order:
                    order._action_cancel()
                picking.message_post(body=Markup(
                    '<strong>%(title)s</strong> %(user)s<br/>%(reason)s'
                ) % {
                    'title': _('Offline delivery rejected by'),
                    'user': self.env.user.name,
                    'reason': capture.review_note,
                })
            capture.write({
                'state': 'rejected',
                'reviewed_by': self.env.user.id,
                'reviewed_on': fields.Datetime.now(),
            })

    def action_retry(self):
        """Re-run a capture that could not be issued, after somebody has fixed
        whatever stopped it -- almost always a missing rate."""
        self._check_dispatcher()
        for capture in self:
            if capture.state != 'failed':
                raise UserError(_('Only a delivery that could not be issued can be retried.'))
        self._try_issue()

    def action_open_note(self):
        self.ensure_one()
        if not self.picking_id:
            raise UserError(_('No delivery note was issued for this capture.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Delivery Note'),
            'res_model': 'stock.picking',
            'res_id': self.picking_id.id,
            'view_mode': 'form',
            'views': [(
                self.env.ref('KSW_water_delivery.view_water_delivery_note_form').id,
                'form',
            )],
            'target': 'current',
        }
