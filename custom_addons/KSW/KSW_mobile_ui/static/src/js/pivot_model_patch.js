import { patch } from "@web/core/utils/patch";
import { PivotModel } from "@web/views/pivot/pivot_model";

// Phone screens (Odoo's "small" breakpoint).
const isPhone = () => window.matchMedia("(max-width: 767.98px)").matches;

// A report on a phone opens as a summary: one row per first grouping
// (vehicle, repair type, technician...) and a single column of totals.
// The desktop cross-tab (rows x months, every row pre-expanded two levels)
// showed one mostly-empty month column with the figures off-screen and ran
// to hundreds of thousands of pixels. Most KSW reports set their grouping
// as search defaults (search_default_groupby_*), so the first level of
// those is kept too. Any row still expands on tap to the next level.
patch(PivotModel.prototype, {
    async load(searchParams) {
        if (isPhone()) {
            const context = { ...searchParams.context, pivot_column_groupby: [] };
            let groupBy = searchParams.groupBy;
            if (groupBy.length) {
                groupBy = groupBy.slice(0, 1);
            } else {
                const rows =
                    context.pivot_row_groupby ||
                    (this.metaData && this.metaData.rowGroupBys) ||
                    [];
                context.pivot_row_groupby = rows.slice(0, 1);
            }
            searchParams = { ...searchParams, context, groupBy };
        }
        return super.load(searchParams);
    },
});
