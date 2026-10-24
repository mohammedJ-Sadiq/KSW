/** @odoo-module **/
// The Dashboards app shows the KSW overview pages next to Odoo's spreadsheets.
// An entry with x_client_action_tag is not loaded as a spreadsheet: it gets its
// own status, which every spreadsheet branch of the action (search bar, share,
// print, figures) already ignores, and the template renders the client action.
import { patch } from "@web/core/utils/patch";
import { registry } from "@web/core/registry";
import { DashboardLoader } from "@spreadsheet_dashboard/bundle/dashboard_action/dashboard_loader_service";
import { SpreadsheetDashboardAction } from "@spreadsheet_dashboard/bundle/dashboard_action/dashboard_action";

export const CLIENT_STATUS = "KswClient";

patch(DashboardLoader.prototype, {
    _getFetchGroupsSpecification() {
        const spec = super._getFetchGroupsSpecification();
        spec.published_dashboard_ids.fields.x_client_action_tag = {};
        return spec;
    },

    async _loadDashboardData(dashboardId) {
        const dashboard = this._getDashboard(dashboardId);
        if (dashboard.data?.x_client_action_tag) {
            dashboard.status = CLIENT_STATUS;
            return;
        }
        return super._loadDashboardData(dashboardId);
    },
});

patch(SpreadsheetDashboardAction.prototype, {
    setup() {
        super.setup();
        this.kswClientStatus = CLIENT_STATUS;
    },

    kswClientComponent(dashboard) {
        return registry.category("actions").get(dashboard.data.x_client_action_tag, null);
    },

    kswClientProps(dashboard) {
        return {
            action: { type: "ir.actions.client", tag: dashboard.data.x_client_action_tag, name: dashboard.data.name,
                      params: {}, context: {} },
            actionId: undefined,
            updateActionState: () => {},
            className: "",
        };
    },
});
