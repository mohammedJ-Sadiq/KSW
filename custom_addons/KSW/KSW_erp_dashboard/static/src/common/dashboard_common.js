/** @odoo-module **/
// Shared pieces of the KSW overview pages: money formatting, opening the
// records behind a figure, the period picker and a small bar chart.
import { Component, onMounted, onWillStart, onWillUnmount, onWillUpdateProps, useRef } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { formatFloat } from "@web/core/utils/numbers";

export const PERIODS = [
    ["month", _t("This month")],
    ["last_month", _t("Last month")],
    ["quarter", _t("This quarter")],
    ["year", _t("This year")],
];

export function money(value, currency, short = true) {
    const digits = short ? 1 : currency?.digits ?? 2;
    const text = formatFloat(value || 0, short ? { humanReadable: true, decimals: digits } : { digits: [16, digits] });
    if (!currency) {
        return text;
    }
    return currency.position === "before" ? `${currency.symbol} ${text}` : `${text} ${currency.symbol}`;
}

export function number(value) {
    return formatFloat(value || 0, { digits: [16, 0] });
}

/** Open a list of records, as a normal Odoo action (breadcrumb back to the dashboard). */
export function openRecords(action, name, model, domain, context = {}) {
    return action.doAction({
        type: "ir.actions.act_window",
        name,
        res_model: model,
        domain,
        views: [[false, "list"], [false, "form"]],
        context: { create: false, ...context },
        target: "current",
    });
}

/** Grouped bar chart (two series), Chart.js as Odoo ships it. */
export class KswBarChart extends Component {
    static template = "KSW_erp_dashboard.BarChart";
    static props = {
        labels: Array,
        series: Array, // [{label, data, color}]
        height: { type: Number, optional: true },
    };

    setup() {
        this.canvas = useRef("canvas");
        onWillStart(() => loadBundle("web.chartjs_lib"));
        onMounted(() => this.draw());
        onWillUpdateProps((next) => this.draw(next));
        onWillUnmount(() => this.chart?.destroy());
    }

    draw(props = this.props) {
        this.chart?.destroy();
        if (!this.canvas.el) {
            return;
        }
        this.chart = new window.Chart(this.canvas.el, {
            type: "bar",
            data: {
                labels: props.labels,
                datasets: props.series.map((s) => ({
                    label: s.label, data: s.data, backgroundColor: s.color, borderRadius: 3, maxBarThickness: 22,
                })),
            },
            options: {
                maintainAspectRatio: false,
                plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } } },
                scales: {
                    x: { grid: { display: false } },
                    y: { ticks: { callback: (v) => formatFloat(v, { humanReadable: true, decimals: 0 }) } },
                },
            },
        });
    }
}
