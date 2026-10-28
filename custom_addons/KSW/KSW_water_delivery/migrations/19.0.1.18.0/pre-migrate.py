"""Client Rates move from ksw.water.rate onto product.pricelist.item.

The list view keeps its xml id but changes model, and KSW_mobile_ui inherits
it with xpaths on fields the old model had (uom_name, source). Validating the
updated parent against that child would fail, so the child goes first;
KSW_mobile_ui recreates it, against the new fields, on its own upgrade.
"""


def migrate(cr, version):
    cr.execute("""
        DELETE FROM ir_ui_view v
         USING ir_model_data d
         WHERE d.model = 'ir.ui.view' AND d.res_id = v.id
           AND d.module = 'KSW_mobile_ui' AND d.name = 'view_water_rate_list_mobile'
    """)
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'KSW_mobile_ui' AND name = 'view_water_rate_list_mobile'
    """)
