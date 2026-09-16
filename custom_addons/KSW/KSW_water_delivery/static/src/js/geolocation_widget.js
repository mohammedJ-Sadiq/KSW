import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

/**
 * Asks the phone where it is and writes it onto the record.
 *
 * Three things this has to survive, because all three are normal rather than
 * exceptional:
 *
 *  1. `navigator.geolocation` only exists in a SECURE CONTEXT. On plain http
 *     over a LAN address every current browser hides it, so it says so plainly
 *     instead of failing silently or looking broken.
 *  2. iOS Safari denies geolocation OUTRIGHT, with no prompt, when Location
 *     Services is off for Safari Websites -- and the error is indistinguishable
 *     from the driver having tapped "Don't Allow". The message therefore names
 *     the setting rather than just reporting a refusal.
 *  3. Asking on load does not always raise the prompt on iOS. A button the
 *     driver taps carries a real user gesture, which does -- so the automatic
 *     attempt is a convenience and the button is always there.
 */
export class KswGeolocationWidget extends Component {
    static template = "KSW_water_delivery.GeolocationWidget";
    static props = { ...standardWidgetProps };

    setup() {
        this.state = useState({ status: "idle", message: "", hint: "" });
        onWillStart(() => this.locate());
    }

    get isRequired() {
        return this.props.record.data.location_rule === "enforce";
    }

    get canRetry() {
        return ["denied", "failed", "idle"].includes(this.state.status);
    }

    async locate() {
        if (!window.isSecureContext || !navigator.geolocation) {
            this.state.status = "unavailable";
            this.state.message = _t(
                "Location needs a secure (https) connection — unavailable on this address."
            );
            this.state.hint = "";
            return;
        }
        this.state.status = "locating";
        this.state.message = _t("Finding your location…");
        this.state.hint = "";
        try {
            const position = await new Promise((resolve, reject) =>
                navigator.geolocation.getCurrentPosition(resolve, reject, {
                    enableHighAccuracy: true,
                    timeout: 15000,
                    maximumAge: 30000,
                })
            );
            await this.props.record.update({
                gps_latitude: position.coords.latitude,
                gps_longitude: position.coords.longitude,
                gps_accuracy: position.coords.accuracy || 0,
            });
            this.state.status = "located";
            this.state.message = _t("Location recorded (±%(m)s m)", {
                m: Math.round(position.coords.accuracy || 0),
            });
            this.state.hint = "";
        } catch (error) {
            const code = error && error.code;
            if (code === 1) {
                this.state.status = "denied";
                this.state.message = _t("Location permission refused.");
                // The refusal is reported identically whether the driver said
                // no or the phone never asked, so name both.
                this.state.hint = _t(
                    "On iPhone check Settings > Privacy & Security > Location Services " +
                        "is on and Safari Websites is set to While Using the App, then tap below."
                );
            } else if (code === 3) {
                this.state.status = "failed";
                this.state.message = _t("Timed out looking for your location.");
                this.state.hint = _t("Move somewhere with a clearer view of the sky and try again.");
            } else {
                this.state.status = "failed";
                this.state.message = _t("Could not get your location.");
                this.state.hint = "";
            }
        }
    }

    onRetry() {
        this.locate();
    }
}

export const kswGeolocationWidget = {
    component: KswGeolocationWidget,
    fieldDependencies: [
        { name: "gps_latitude", type: "float" },
        { name: "gps_longitude", type: "float" },
        { name: "gps_accuracy", type: "float" },
        { name: "location_rule", type: "selection" },
    ],
};

registry.category("view_widgets").add("ksw_geolocation", kswGeolocationWidget);
