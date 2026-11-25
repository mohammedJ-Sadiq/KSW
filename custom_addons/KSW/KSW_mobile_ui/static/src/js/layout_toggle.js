import { reactive, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { user } from "@web/core/user";
import { patch } from "@web/core/utils/patch";
import { ControlPanel } from "@web/search/control_panel/control_panel";
import { session } from "@web/session";
import { ListRenderer } from "@web/views/list/list_renderer";

/**
 * Phone lists come in two layouts, one button apart:
 *
 * - Cards: one record per card, every column labelled. Right for a handful
 *   of records and for approving (the default, mobile.scss).
 * - Table: one record per line, a real grid that scrolls sideways with the
 *   first column and the header pinned. Right for reviewing hundreds of
 *   records (300 payslips), the way a spreadsheet app does it on a phone.
 *
 * A screen that has its own kanban cards uses those as "Cards", so its list
 * is always the table. The choice is remembered per screen on this device
 * (browser storage: a per-viewer convenience, losing it costs one tap).
 */
const STORAGE_KEY = `ksw_m_layout_${session.db}_${user.userId}`;

function load() {
    try {
        return JSON.parse(browser.localStorage.getItem(STORAGE_KEY) || "{}");
    } catch {
        return {};
    }
}

export const kswLayouts = reactive({ byScreen: load() });

function screenKey(config) {
    return config?.actionId ? String(config.actionId) : null;
}

function hasView(config, type) {
    return (config?.viewSwitcherEntries || []).some((v) => v.type === type);
}

export function kswLayoutFor(config) {
    const key = screenKey(config);
    if (hasView(config, "kanban")) {
        // The kanban is the cards: the list is only ever the table.
        return "table";
    }
    return (key && kswLayouts.byScreen[key]) || "cards";
}

function remember(config, layout) {
    const key = screenKey(config);
    if (!key) {
        return;
    }
    kswLayouts.byScreen[key] = layout;
    syncContext();
    try {
        browser.localStorage.setItem(STORAGE_KEY, JSON.stringify(kswLayouts.byScreen));
    } catch {
        // storage unavailable: the choice lasts until the page reloads
    }
}

// The button: next to the pager, on list and kanban screens of an action.
patch(ControlPanel.prototype, {
    setup() {
        super.setup(...arguments);
        this.kswLayouts = useState(kswLayouts);
    },

    get kswShowLayoutToggle() {
        const config = this.env.config;
        return (
            this.env.isSmall &&
            screenKey(config) &&
            ["list", "kanban"].includes(config.viewType) &&
            (config.viewType === "kanban" ? hasView(config, "list") : true)
        );
    },

    /** The layout the button switches TO. */
    get kswNextLayout() {
        const config = this.env.config;
        if (config.viewType === "kanban") {
            return "table";
        }
        return kswLayoutFor(config) === "table" ? "cards" : "table";
    },

    kswToggleLayout() {
        const config = this.env.config;
        const next = this.kswNextLayout;
        remember(config, next);
        if (config.viewType === "kanban") {
            this.actionService.switchView("list");
        } else if (next === "cards" && hasView(config, "kanban")) {
            this.actionService.switchView("kanban");
        }
        // A list without kanban re-renders from the reactive store.
    },
});

// The list renders the remembered layout (mobile.scss keys on the class).
patch(ListRenderer.prototype, {
    setup() {
        super.setup(...arguments);
        this.kswLayouts = useState(kswLayouts);
    },

    get kswTableLayout() {
        return (
            this.env.isSmall &&
            this.env.config?.viewType === "list" &&
            kswLayoutFor(this.env.config) === "table"
        );
    },
});

// A screen last left as a table opens as a table again: Odoo opens a phone
// on the kanban first (mobile_view_mode), so the server is told which screens
// to open on the list instead (models/ir_actions.py). Through the user
// context, so menus, links and a page reload all follow it.
function syncContext() {
    const ids = Object.entries(kswLayouts.byScreen)
        .filter(([, layout]) => layout === "table")
        .map(([key]) => Number(key))
        .filter(Boolean);
    user.updateContext({ ksw_m_table_actions: ids });
}
syncContext();
