import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { useBus } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { FormController } from "@web/views/form/form_controller";

/**
 * Ask before leaving an unsaved form instead of saving it silently.
 *
 * Stock Odoo saves a dirty form from four places nobody thinks of as "Save":
 * navigating away (breadcrumb / menu), the record pager, switching browser
 * tab, and closing or reloading the page. For a time-off request, saving a
 * new record *is* submitting it, and a supervisor half-way through a
 * commission component gets lines committed they never confirmed.
 *
 * For the models below, those four paths no longer save: in-app navigation
 * and the pager ask "leave without saving?" (Discard -> changes dropped,
 * Stay -> nothing happens), closing the tab raises the browser's own
 * leave-page prompt, and switching tab does nothing. A record the user has
 * already saved is not dirty, so no prompt is shown. Explicit buttons
 * (Save, workflow buttons) keep saving as before.
 */
const NO_AUTOSAVE_MODELS = new Set([
    "hr.leave",
    "ksw.pay.batch",
    "ksw.pay.submission",
    "ksw.pay.entry",
]);

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        // An input being typed in is only committed to the record on blur;
        // track it like FormStatusIndicator does so a tab close mid-typing
        // still counts as unsaved (beforeunload can't await a commit).
        this._kswFieldDirty = false;
        useBus(this.model.bus, "FIELD_IS_DIRTY", (ev) => (this._kswFieldDirty = ev.detail));
    },

    get _kswNoAutosave() {
        return NO_AUTOSAVE_MODELS.has(this.props.resModel);
    },

    _kswConfirmDiscard() {
        return new Promise((resolve) => {
            this.dialogService.add(
                ConfirmationDialog,
                {
                    title: _t("Unsaved changes"),
                    body: _t(
                        "You have unsaved changes. Are you sure you want to leave without saving? Your changes will be discarded."
                    ),
                    confirmLabel: _t("Discard changes"),
                    confirmClass: "btn-danger",
                    confirm: async () => {
                        await this.model.root.discard();
                        this._kswFieldDirty = false;
                        resolve(true);
                    },
                    cancelLabel: _t("Stay here"),
                    cancel: () => resolve(false),
                },
                { onClose: () => resolve(false) }
            );
        });
    },

    async beforeLeave({ forceLeave } = {}) {
        if (!this._kswNoAutosave || forceLeave) {
            return super.beforeLeave(...arguments);
        }
        if (!(await this.model.root.isDirty())) {
            return true;
        }
        return this._kswConfirmDiscard();
    },

    async onPagerUpdate() {
        if (this._kswNoAutosave && (await this.model.root.isDirty())) {
            if (!(await this._kswConfirmDiscard())) {
                return;
            }
        }
        return super.onPagerUpdate(...arguments);
    },

    beforeVisibilityChange() {
        if (!this._kswNoAutosave) {
            return super.beforeVisibilityChange(...arguments);
        }
    },

    beforeUnload(ev) {
        if (!this._kswNoAutosave) {
            return super.beforeUnload(...arguments);
        }
        // Browsers only allow their own generic prompt here; the custom
        // dialog above can't be shown while the page is unloading.
        if (this.model.root.dirty || this._kswFieldDirty) {
            ev.preventDefault();
            ev.returnValue = "";
        }
    },

    async discard() {
        await super.discard(...arguments);
        this._kswFieldDirty = false;
    },
});
