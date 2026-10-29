/** A text column that opens the document behind the row.
 *
 * Used on the Stock Movement report for the delivery note and the invoice
 * numbers: the click calls the row's method (options.method), which returns
 * the ksw_open_record_dialog action, so the document opens in a pop-up that
 * can be expanded to the full page. Plain text when the row has nothing to
 * open (options.when names the field that must be set).
 */
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class RecordLinkField extends Component {
    static template = "KSW_stock_movement.RecordLinkField";
    static props = {
        ...standardFieldProps,
        method: { type: String },
        when: { type: String, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
    }

    get value() {
        return this.props.record.data[this.props.name] || "";
    }

    get linked() {
        const when = this.props.when;
        return Boolean(this.value) && (!when || Boolean(this.props.record.data[when]));
    }

    async onClick(ev) {
        // The list would otherwise open the report row itself.
        ev.preventDefault();
        ev.stopPropagation();
        const action = await this.orm.call(
            this.props.record.resModel, this.props.method, [[this.props.record.resId]]);
        if (action) {
            await this.action.doAction(action);
        }
    }
}

registry.category("fields").add("ksw_record_link", {
    component: RecordLinkField,
    supportedTypes: ["char"],
    extractProps: ({ options }) => ({ method: options.method, when: options.when }),
});
