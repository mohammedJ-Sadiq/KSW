from odoo import models


class IrActionsActWindow(models.Model):
    _inherit = 'ir.actions.act_window'

    def _get_action_dict(self):
        """A screen the user switched to Table on their phone opens as the list.

        The phone client sends those action ids in its context
        (static/src/js/layout_toggle.js). Odoo opens `mobile_view_mode`
        (kanban by default) first on a phone, and the client cannot change that
        once the action is loaded without a visible second load.
        """
        result = super()._get_action_dict()
        table_ids = self.env.context.get('ksw_m_table_actions')
        if (
            result
            and isinstance(table_ids, list)
            and self.id in table_ids
            and 'list' in (result.get('view_mode') or '').split(',')
        ):
            result['mobile_view_mode'] = 'list'
        return result
