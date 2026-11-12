import logging

_logger = logging.getLogger(__name__)

# HR-12 moved to KSW_vehicle_custody (19.0.1.6.0). Its views here extended the
# transfer and workshop forms with fields this version no longer has, and Odoo
# validates those parents before it would clean up the orphans, so the upgrade
# aborts. Drop them first; the module's own cleanup removes the rest.
_STALE_VIEWS = (
    'view_ksw_fleet_move_form_handover', 'view_ksw_workshop_request_form_handover',
    'view_ksw_vehicle_handover_form', 'view_ksw_vehicle_handover_list',
    'view_ksw_vehicle_handover_search', 'view_ksw_vehicle_inspection_item_list',
)


def migrate(cr, version):
    cr.execute("""
        DELETE FROM ir_ui_view WHERE id IN (
            SELECT res_id FROM ir_model_data
             WHERE module = 'KSW_fleet_allocation' AND model = 'ir.ui.view' AND name IN %s)
        RETURNING id
    """, [_STALE_VIEWS])
    _logger.info('KSW_fleet_allocation: dropped %s HR-12 views moved to KSW_vehicle_custody', cr.rowcount)
