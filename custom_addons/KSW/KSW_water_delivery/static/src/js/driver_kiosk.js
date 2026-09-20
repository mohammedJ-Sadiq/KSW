import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { browser } from "@web/core/browser/browser";
import { isDisplayStandalone } from "@web/core/browser/feature_detection";

/** True when the page is running as an installed app rather than in a tab.
 *  `display-mode: standalone` is the standard signal; `navigator.standalone` is
 *  the older iOS one, kept because a home-screen web app on an iPhone did not
 *  match the media query before iOS 16.4 — and the drivers' phones are not
 *  something we control. */
function launchedFromTheAppIcon() {
    return (
        isDisplayStandalone() ||
        // iOS before 16.4 did not match the media query for a home-screen web
        // app, and the drivers' phones are not something we control.
        browser.navigator.standalone === true ||
        // Some launchers open an installed app fullscreen or with a minimal
        // toolbar rather than in the standalone display mode.
        browser.matchMedia("(display-mode: fullscreen)").matches ||
        browser.matchMedia("(display-mode: minimal-ui)").matches
    );
}

/**
 * Turns the web client into a single-app screen.
 *
 * Two independent reasons to do it, because they answer different questions:
 *
 *  1. WHO — a driver is an internal user, and the other KSW modules push their
 *     baseline groups onto `base.group_user`, so a pure driver can see ten apps
 *     (Discuss, Calendar, Employees, Time Off, Workshop, Deductions, Helpdesk,
 *     Dashboards, Apps and this one). None of them are his job.
 *
 *  2. WHERE — anyone who opens the installed "Water Delivery" icon has said
 *     which app they want. Inside that window there should be no way out to the
 *     rest of Odoo, even for a dispatcher or a billing clerk who legitimately
 *     needs everything else in a normal browser tab. The same account therefore
 *     behaves differently in the two places, on purpose: the home-screen icon
 *     is the app, the browser is Odoo.
 *
 * Odoo's navbar hides the switcher itself when `isScopedApp`, but that is
 * `location.href.includes("/scoped_app")` — true only on the install page, not
 * in the installed app, whose start URL is the plain action path.
 */
const driverKioskService = {
    dependencies: [],
    async start() {
        const [isDriver, isDispatcher, isBilling] = await Promise.all([
            user.hasGroup("KSW_water_delivery.group_water_driver"),
            user.hasGroup("KSW_water_delivery.group_water_dispatcher"),
            user.hasGroup("KSW_water_delivery.group_water_billing"),
        ]);
        if (!isDriver) {
            // Nothing to lock down for someone with no part in this app.
            return;
        }
        const isDriverOnly = !isDispatcher && !isBilling;
        if (isDriverOnly || launchedFromTheAppIcon()) {
            document.body.classList.add("o_ksw_driver_kiosk");
        }
    },
};

registry.category("services").add("ksw_driver_kiosk", driverKioskService);
