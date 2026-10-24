/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { KswBarChart, PERIODS, money, number, openRecords } from "@KSW_erp_dashboard/common/dashboard_common";

export class KswWorkshopDashboard extends Component {
    static template = "KSW_workshop_dashboard.Workshop";
    static components = { KswBarChart };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.title = _t("Workshop Overview");
        this.standardLabel = _t("All Requests");
        this.labels = {
            open: _t("Open requests"), triage: _t("Waiting for triage"), staleTriage: _t("Waiting for triage over 2 days"),
            inProgress: _t("In the workshop"), received: _t("Received"), completed: _t("Completed"),
            repeat: _t("Repeat repairs"), openByAge: _t("Open requests by age"),
            costly: _t("Completed work with a cost"),
        };
        this.trendTitles = {
            day: _t("Received and completed, by day"), week: _t("Received and completed, by week"),
            month: _t("Received and completed, by month"),
        };
        this.state = useState({ period: "month", data: null, loading: true });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("ksw.workshop.dashboard", "get_workshop_data", [this.state.period]);
        this.state.loading = false;
    }

    setPeriod(period) {
        this.state.period = period;
        this.load();
    }

    m(value, short = true) {
        return money(value, this.state.data?.currency, short);
    }

    n(value) {
        return number(value);
    }

    change(current, previous) {
        if (!previous) {
            return "";
        }
        const pct = ((current - previous) / Math.abs(previous)) * 100;
        return `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(0)}%`;
    }

    width(value, rows, key = "count") {
        const max = Math.max(...rows.map((r) => Math.abs(r[key])), 1);
        return `${Math.round((Math.abs(value) / max) * 100)}%`;
    }

    open(name, domain) {
        return openRecords(this.action, name, "ksw.workshop.request", domain);
    }

    openStandard() {
        return this.action.doAction("KSW_workshop.action_ksw_workshop_request_all");
    }

    get trendTitle() {
        return this.trendTitles[this.state.data.trend_unit];
    }

    get trendLabels() {
        return this.state.data.trend.map((t) => t.label);
    }

    get trendSeries() {
        const t = this.state.data.trend;
        return [
            { label: this.labels.received, data: t.map((r) => r.received), color: "#3b82f6" },
            { label: this.labels.completed, data: t.map((r) => r.completed), color: "#10b981" },
        ];
    }
}

registry.category("actions").add("ksw_workshop_dashboard", KswWorkshopDashboard);
