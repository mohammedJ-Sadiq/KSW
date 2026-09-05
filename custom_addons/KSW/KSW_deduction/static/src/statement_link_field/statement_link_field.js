/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component } from "@odoo/owl";

/**
 * Renders a Char field as plain text, or — when a companion id field on
 * the same record is set — as a blue/hoverable link that calls an object
 * method (which returns an ir.actions.act_window dict) to drill into the
 * document behind it.
 *
 * Used on the Statement of Account's Description column: the companion
 * field is `payslip_id`, only ever set on a "credit" row settled through
 * payroll. The raw payslip is never fetched to render the link (only its
 * id, already on the record) — the server method itself checks access
 * and raises a readable error, avoiding a crash for a non-payroll user
 * whose record rules would block a many2one widget on `payslip_id` from
 * even loading its display name.
 */
export class KswStatementLinkField extends Component {
    static template = "KSW_deduction.StatementLinkField";
    static props = {
        ...standardFieldProps,
        idField: { type: String },
        method: { type: String },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
    }

    get value() {
        return this.props.record.data[this.props.name] || "";
    }

    get isLink() {
        return Boolean(this.props.record.data[this.props.idField]);
    }

    async onClick() {
        if (!this.isLink) {
            return;
        }
        const action = await this.orm.call(
            this.props.record.resModel,
            this.props.method,
            [[this.props.record.resId]]
        );
        if (action) {
            this.action.doAction(action);
        }
    }
}

export const kswStatementLinkField = {
    component: KswStatementLinkField,
    supportedTypes: ["char"],
    extractProps: ({ options }) => ({
        idField: options.id_field,
        method: options.method,
    }),
};

registry.category("fields").add("ksw_statement_link", kswStatementLinkField);
