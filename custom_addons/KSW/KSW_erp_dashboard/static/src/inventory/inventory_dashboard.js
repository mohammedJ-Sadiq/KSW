/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { KswBarChart, PERIODS, money, number, openRecords } from "../common/dashboard_common";

export class KswInventoryDashboard extends Component {
    static template = "KSW_erp_dashboard.Inventory";
    static components = { KswBarChart };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.title = _t("Inventory Overview");
        this.standardLabel = _t("Operations");
        this.labels = {
            stock: _t("Stock on hand"), dead: _t("Dead stock"), slow: _t("Slow moving"),
            low: _t("Running low"), out: _t("Out of stock"),
        };
        this.state = useState({ period: "month", data: null, loading: true });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("ksw.erp.dashboard", "get_inventory_data", [this.state.period]);
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

    width(value, rows, key = "value") {
        const max = Math.max(...rows.map((r) => Math.abs(r[key])), 1);
        return `${Math.round((Math.abs(value) / max) * 100)}%`;
    }

    openProducts(name, ids) {
        return openRecords(this.action, name, "product.product", [["id", "in", ids]]);
    }

    openStock(name, domain) {
        return openRecords(this.action, name, "stock.quant",
            [["location_id.usage", "=", "internal"], ...domain]);
    }

    openOperation(op, state) {
        const states = { ready: ["assigned"], waiting: ["confirmed", "waiting"], late: ["assigned", "confirmed", "waiting"] };
        const domain = [["picking_type_id", "=", op.id], ["state", "in", states[state]]];
        if (state === "late") {
            domain.push(["scheduled_date", "<", luxon.DateTime.now().toFormat("yyyy-MM-dd HH:mm:ss")]);
        }
        return openRecords(this.action, op.name, "stock.picking", domain);
    }

    openIssues(row) {
        return openRecords(this.action, row.name, "stock.move",
            [["state", "=", "done"], ["analytic_distribution", "in", [row.id]]]);
    }

    openStandard() {
        return this.action.doAction("stock.stock_picking_type_action");
    }

    get trendLabels() {
        return this.state.data.trend.map((t) => t.label);
    }

    get trendSeries() {
        const t = this.state.data.trend;
        return [
            { label: _t("Received"), data: t.map((r) => r.received), color: "#10b981" },
            { label: _t("Issued"), data: t.map((r) => r.issued), color: "#6366f1" },
        ];
    }
}

registry.category("actions").add("ksw_inventory_dashboard", KswInventoryDashboard);
