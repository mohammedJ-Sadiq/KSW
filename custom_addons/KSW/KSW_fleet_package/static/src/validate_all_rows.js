import { Notebook } from "@web/core/notebook/notebook";
import { evaluateExpr } from "@web/core/py_js/py";
import { patch } from "@web/core/utils/patch";
import { Record } from "@web/model/relational_model/record";

/**
 * The web client only validates x2many rows the user edited. Rows the server
 * created (the vehicles of a released employee) are never touched, so an
 * unanswered required column passes the client and comes back as a bare
 * server error. An x2many with `context="{'ksw_validate_all_rows': True}"`
 * validates every row: the cells turn red, the notebook tab is flagged, and
 * the form opens that tab so the user lands on the field to fill.
 */
patch(Record.prototype, {
    _checkValidity({ silent, displayNotification, removeInvalidOnly } = {}) {
        const fieldNames = this._kswValidateAllRowsFields();
        if (!fieldNames.length) {
            return super._checkValidity(...arguments);
        }
        const wasInvalid = new Set(fieldNames.filter((f) => this._unsetRequiredFields.has(f)));
        let isValid = super._checkValidity({ silent, removeInvalidOnly });
        for (const fieldName of fieldNames) {
            const rowsValid = this.data[fieldName].records.every((r) =>
                r._checkValidity({ silent: silent || removeInvalidOnly, removeInvalidOnly })
            );
            if (rowsValid || (removeInvalidOnly && !wasInvalid.has(fieldName))) {
                continue;
            }
            isValid = false;
            if (!silent) {
                this._unsetRequiredFields.add(fieldName);
                this._invalidFields.add(fieldName);
            }
        }
        if (!isValid && displayNotification) {
            this._closeInvalidFieldsNotification = this._displayInvalidFieldNotification();
        }
        return isValid;
    },

    _kswValidateAllRowsFields() {
        return Object.keys(this.activeFields).filter((fieldName) => {
            const activeField = this.activeFields[fieldName];
            if (
                !["one2many", "many2many"].includes(this.fields[fieldName].type) ||
                !activeField.context?.includes("ksw_validate_all_rows") ||
                this._isInvisible(fieldName) ||
                this._isReadonly(fieldName)
            ) {
                return false;
            }
            return Boolean(evaluateExpr(activeField.context, this.evalContext).ksw_validate_all_rows);
        });
    },
});

patch(Notebook.prototype, {
    computeInvalidPages() {
        const before = this.invalidPages;
        super.computeInvalidPages();
        const root = this.env.model?.root;
        if (!root?._kswValidateAllRowsFields || this.invalidPages.has(this.state.currentPage)) {
            return;
        }
        const flagged = root._kswValidateAllRowsFields();
        const target = this.navItems.find(
            ([index, page]) =>
                this.invalidPages.has(index) &&
                !before.has(index) &&
                page.fieldNames?.some((fieldName) => flagged.includes(fieldName))
        );
        if (target) {
            // Not during the render itself: switching the page re-renders.
            Promise.resolve().then(() => this.activatePage(target[0]));
        }
    },
});
