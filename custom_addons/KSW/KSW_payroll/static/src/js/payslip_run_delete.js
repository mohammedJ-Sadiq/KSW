import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { Dialog } from "@web/core/dialog/dialog";
import { Component } from "@odoo/owl";
import { FormController } from "@web/views/form/form_controller";
import { ListController } from "@web/views/list/list_controller";

/**
 * Deleting a payslip batch asks what to do with its payslips.
 *
 * Stock delete removes only the batch and leaves its payslips behind with
 * an empty batch. For hr.payslip.run, the Delete action (form gear menu and
 * list "Delete") now offers three choices when the batch holds payslips:
 *   1. delete the batch and its payslips (hr.payslip.run.action_unlink_with_payslips),
 *   2. delete only the batch and keep the payslips (the stock unlink),
 *   3. discard.
 * A batch with no payslips keeps the stock confirmation.
 */
const MODEL = "hr.payslip.run";

export class PayslipRunDeleteDialog extends Component {
    static template = "KSW_payroll.PayslipRunDeleteDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        runCount: Number,
        slipCount: Number,
        deleteAll: Function,
        deleteBatchOnly: Function,
    };

    get body() {
        if (this.props.runCount > 1) {
            return _t(
                "This will delete the %(runs)s selected batches and their %(slips)s attached payslips.",
                { runs: this.props.runCount, slips: this.props.slipCount }
            );
        }
        return _t("This will delete the batch and its %(slips)s attached payslips.", {
            slips: this.props.slipCount,
        });
    }

    async run(callback) {
        this.props.close();
        await callback();
    }
}

async function countSlips(orm, runIds) {
    if (!runIds.length) {
        return 0;
    }
    return orm.searchCount("hr.payslip", [["payslip_run_id", "in", runIds]]);
}

patch(FormController.prototype, {
    async deleteRecord() {
        const record = this.model.root;
        if (this.props.resModel !== MODEL || !record.resId) {
            return super.deleteRecord(...arguments);
        }
        const slipCount = await countSlips(this.orm, [record.resId]);
        if (!slipCount) {
            return super.deleteRecord(...arguments);
        }
        this.dialogService.add(PayslipRunDeleteDialog, {
            runCount: 1,
            slipCount,
            deleteAll: async () => {
                await this.orm.call(MODEL, "action_unlink_with_payslips", [[record.resId]]);
                this.env.config.historyBack();
            },
            deleteBatchOnly: () => this.deleteConfirmationDialogProps.confirm(),
        });
    },
});

patch(ListController.prototype, {
    async onDeleteSelectedRecords() {
        if (this.props.resModel !== MODEL) {
            return super.onDeleteSelectedRecords(...arguments);
        }
        const runIds = await this.model.root.getResIds(true);
        const slipCount = await countSlips(this.orm, runIds);
        if (!slipCount) {
            return super.onDeleteSelectedRecords(...arguments);
        }
        this.dialogService.add(PayslipRunDeleteDialog, {
            runCount: runIds.length,
            slipCount,
            deleteAll: async () => {
                await this.orm.call(MODEL, "action_unlink_with_payslips", [runIds]);
                await this.model.load();
            },
            deleteBatchOnly: () => this.model.root.deleteRecords(),
        });
    },
});
