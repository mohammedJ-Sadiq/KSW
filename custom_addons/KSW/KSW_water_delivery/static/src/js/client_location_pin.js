import { Component, onWillUnmount, useState } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

// The server's limit (res_partner.PIN_MAX_ACCURACY_M). It refuses anything
// rougher anyway; waiting for it here just saves a pointless round trip.
const PIN_MAX_ACCURACY_M = 50;
const PIN_WINDOW_MS = 60000;

/**
 * "📍 Send my location for this client", on the Odoo New Delivery form.
 *
 * Under the enforce rule a client with no approved location cannot receive a
 * note, so this is the driver's only move at such a gate. It sends a request
 * for a dispatcher to approve -- the same one the offline app sends -- and
 * never locates the client itself.
 *
 * Always from a FRESH fix, never the one the form took on opening: that may
 * be minutes old and from the depot, and the approved pin becomes the centre
 * of the radius every later note at this site is checked against.
 */
export class KswClientLocationPin extends Component {
    static template = "KSW_water_delivery.ClientLocationPin";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({ busy: false, accuracy: null, sentFor: [] });
        this.watchId = null;
        onWillUnmount(() => this.stopWatching());
    }

    get data() {
        return this.props.record.data;
    }

    get partnerId() {
        return this.data.partner_id && this.data.partner_id.id;
    }

    get applies() {
        return this.data.location_rule === "enforce" && Boolean(this.partnerId);
    }

    get isPending() {
        return this.data.partner_location_state === "pending"
            || this.state.sentFor.includes(this.partnerId);
    }

    get needsPin() {
        return this.applies && this.data.partner_location_state === "missing" && !this.isPending;
    }

    stopWatching() {
        if (this.watchId !== null) {
            navigator.geolocation.clearWatch(this.watchId);
            this.watchId = null;
        }
    }

    onSend() {
        const name = this.data.partner_id.display_name;
        this.dialog.add(ConfirmationDialog, {
            title: _t("Send this client's location?"),
            body: _t(
                "Your current position will be sent as the location of %(client)s. " +
                    "Only do this standing at the client's site, outside the cab. " +
                    "The office approves it before notes can be issued for this client.",
                { client: name }
            ),
            confirmLabel: _t("Send location"),
            confirm: () => this.locateAndSend(),
            cancel: () => {},
        });
    }

    locateAndSend() {
        if (!window.isSecureContext || !navigator.geolocation) {
            this.notification.add(
                _t("Location needs a secure (https) connection — unavailable on this address."),
                { type: "danger" }
            );
            return;
        }
        const partnerId = this.partnerId;
        this.state.busy = true;
        this.state.accuracy = null;
        const started = Date.now();
        this.stopWatching();
        this.watchId = navigator.geolocation.watchPosition(
            async (position) => {
                const accuracy = position.coords.accuracy || 0;
                this.state.accuracy = Math.round(accuracy);
                if (accuracy && accuracy <= PIN_MAX_ACCURACY_M) {
                    this.stopWatching();
                    await this.send(partnerId, position.coords);
                } else if (Date.now() - started > PIN_WINDOW_MS) {
                    this.stopWatching();
                    this.state.busy = false;
                    this.notification.add(
                        _t("The fix is still too rough (±%(m)s m). Step out of the cab, away from the tank, and try again.",
                            { m: Math.round(accuracy) }),
                        { type: "warning" }
                    );
                }
            },
            (error) => {
                this.stopWatching();
                this.state.busy = false;
                this.notification.add(
                    error && error.code === 1
                        ? _t("Location permission refused. Allow location for this site and try again.")
                        : _t("Could not get your location. Move somewhere with a clearer view of the sky."),
                    { type: "danger" }
                );
            },
            { enableHighAccuracy: true, timeout: PIN_WINDOW_MS, maximumAge: 0 }
        );
    }

    async send(partnerId, coords) {
        try {
            const result = await this.orm.call(
                "ksw.water.delivery.wizard",
                "action_send_client_location",
                [partnerId, coords.latitude, coords.longitude, coords.accuracy || 0]
            );
            if (result.status === "pending") {
                this.state.sentFor.push(partnerId);
                this.notification.add(
                    _t("Location sent for %(client)s — waiting for the office to approve it.",
                        { client: result.name }),
                    { type: "success" }
                );
            } else {
                this.notification.add(result.message || _t("The location was not accepted."), {
                    type: "warning",
                });
            }
        } finally {
            this.state.busy = false;
        }
    }
}

export const kswClientLocationPin = {
    component: KswClientLocationPin,
    fieldDependencies: [
        { name: "partner_id", type: "many2one" },
        { name: "partner_location_state", type: "selection" },
        { name: "location_rule", type: "selection" },
    ],
};

registry.category("view_widgets").add("ksw_client_location_pin", kswClientLocationPin);
