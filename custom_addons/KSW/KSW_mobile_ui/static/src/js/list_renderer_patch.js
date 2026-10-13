import { patch } from "@web/core/utils/patch";
import { ListRenderer } from "@web/views/list/list_renderer";

const MOBILE_HIDE = "ksw_m_hide";

function isMobileHidden(node) {
    return (node.className || "").split(" ").includes(MOBILE_HIDE);
}

// On a phone, drop what a view marks as low priority (class="ksw_m_hide").
// Doing it here rather than in CSS keeps the header, the cells and the
// totals row aligned. Adjacent buttons share one column, so buttons are
// filtered one by one and the column goes only when none are left.
patch(ListRenderer.prototype, {
    getActiveColumns() {
        const columns = super.getActiveColumns(...arguments);
        if (!this.env.isSmall) {
            return columns;
        }
        const kept = [];
        for (const col of columns) {
            if (col.type === "button_group") {
                const buttons = col.buttons.filter((b) => !isMobileHidden(b));
                if (buttons.length === col.buttons.length) {
                    kept.push(col);
                } else if (buttons.length) {
                    kept.push({ ...col, buttons });
                }
            } else if (!isMobileHidden(col)) {
                kept.push(col);
            }
        }
        return kept;
    },
});
