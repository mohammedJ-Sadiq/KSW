/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import { KswBarChart, PERIODS, money, number, openRecords } from "../common/dashboard_common";

export class KswFinanceDashboard extends Component {
    static template = "KSW_erp_dashboard.Finance";
    static components = { KswBarChart };
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.title = _t("Accounting Overview");
        this.standardLabel = _t("Journals");
        this.labels = {
            receivablesAging: _t("Receivables aging"), payablesAging: _t("Payables aging"),
            revenue: _t("Revenue"), expenses: _t("Expenses"), profit: _t("Profit"),
            cash: _t("Cash position"), receivables: _t("Receivables"), payables: _t("Payables"),
            overdueReceivables: _t("Overdue receivables"), overduePayables: _t("Overdue payables"),
        };
        this.trendTitles = {
            day: _t("Revenue and expenses, by day"), week: _t("Revenue and expenses, by week"),
            month: _t("Revenue and expenses, by month"),
        };
        this.state = useState({ period: "month", data: null, loading: true });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("ksw.erp.dashboard", "get_finance_data", [this.state.period]);
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

    bucketWidth(bucket, aging) {
        const max = Math.max(...aging.buckets.map((b) => Math.abs(b.amount)), 1);
        return `${Math.round((Math.abs(bucket.amount) / max) * 100)}%`;
    }

    openLines(name, domain) {
        return openRecords(this.action, name, "account.move.line", domain, { search_default_group_by_partner: 0 });
    }

    openTodo(item) {
        return openRecords(this.action, item.label, item.model, item.domain);
    }

    openStandard() {
        return this.action.doAction("account.open_account_journal_dashboard_kanban");
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
            { label: _t("Revenue"), data: t.map((r) => r.revenue), color: "#3b82f6" },
            { label: _t("Expenses"), data: t.map((r) => r.expenses), color: "#f59e0b" },
        ];
    }
}

registry.category("actions").add("ksw_finance_dashboard", KswFinanceDashboard);
