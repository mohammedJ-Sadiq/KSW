/** Open one record in a pop-up that can be expanded to the full page.
 *
 * Odoo's own FormViewDialog has that expand button, but an ordinary
 * `target: 'new'` action does not turn it on. The Stock Movement report's
 * Delivery / Invoice buttons return this client action instead.
 */
import { registry } from "@web/core/registry";
import { FormViewDialog } from "@web/views/view_dialogs/form_view_dialog";

function openRecordDialog(env, action) {
    const params = action.params || {};
    env.services.dialog.add(FormViewDialog, {
        resModel: params.res_model,
        resId: params.res_id,
        title: params.title,
        viewId: params.view_id || false,
        readonly: true,
        canExpand: true,
        expandedFormRef: params.form_view_ref || undefined,
        context: params.form_view_ref ? { form_view_ref: params.form_view_ref } : {},
    });
}

registry.category("actions").add("ksw_open_record_dialog", openRecordDialog);
