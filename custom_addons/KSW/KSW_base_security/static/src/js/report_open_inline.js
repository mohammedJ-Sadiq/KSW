import { registry } from "@web/core/registry";
import { browser } from "@web/core/browser/browser";
import { getReportUrl } from "@web/webclient/actions/reports/utils";

/**
 * Open KSW PDF reports in the browser instead of downloading them.
 *
 * Odoo's default handling of an `ir.actions.report` of type `qweb-pdf` is
 * `downloadReport()` -> `/report/download`, which sends the file with
 * `Content-Disposition: attachment`: the PDF lands in the Downloads folder
 * and the user never gets to look at it first. Every report below is a
 * document an HR / accounting user reads on screen far more often than they
 * archive it (vacation settlement, leave approval sheet, payslip, statement
 * of account), so they are sent to `/report/pdf/...` in a new tab instead --
 * that route returns the very same PDF inline, and the browser's own viewer
 * provides the download and print buttons.
 *
 * This deliberately keeps the real PDF rather than the `qweb-html` preview:
 * the paper format, page breaks and the company header/footer are part of
 * these documents, and the HTML render has none of them.
 */

// Reports shipped by a KSW_* module are included by prefix. The payslip
// reports are declared by om_hr_payroll (KSW_payroll only overrides their
// templates), so they are listed by name.
const EXTRA_INLINE_REPORTS = new Set([
    "om_hr_payroll.report_payslip",
    "om_hr_payroll.report_payslip_details",
]);

function opensInline(reportName) {
    return (
        typeof reportName === "string" &&
        (reportName.startsWith("KSW_") || EXTRA_INLINE_REPORTS.has(reportName))
    );
}

registry
    .category("ir.actions.report handlers")
    .add("ksw_open_pdf_inline", (action) => {
        if (action.report_type !== "qweb-pdf" || !opensInline(action.report_name)) {
            // Not ours: let Odoo download it the usual way.
            return false;
        }
        // `getReportUrl` carries the wizard payload (`action.data`) and the
        // `active_ids` of a record-bound report, so this works for both the
        // Print menu and the wizards.
        const win = browser.open(getReportUrl(action, "pdf"), "_blank");
        // A blocked pop-up returns null. Returning false then falls through
        // to the stock download path, so the user still gets the document.
        return Boolean(win);
    });
