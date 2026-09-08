from odoo.tests.common import TransactionCase


class HelpdeskCommon(TransactionCase):
    """Shared fixture: two IT Team agents, a manager and one direct report.

    Notification mails are queued rather than pushed through SMTP
    (``mail_notify_force_send=False``); the mail.message rows the assertions
    read are created either way, and nothing tries to reach a mail server.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(
            cls.env.context,
            mail_notify_force_send=False,
            no_reset_password=True,
        ))

        cls.agent_group = cls.env.ref('KSW_helpdesk.group_helpdesk_agent')
        cls.user_group = cls.env.ref('KSW_helpdesk.group_helpdesk_user')
        cls.internal_group = cls.env.ref('base.group_user')

        cls.stage_new = cls.env.ref('KSW_helpdesk.stage_new')
        cls.stage_in_progress = cls.env.ref('KSW_helpdesk.stage_in_progress')
        cls.stage_closed = cls.env.ref('KSW_helpdesk.stage_closed')

        cls.cat_hardware = cls.env.ref('KSW_helpdesk.ticket_category_hardware')
        cls.cat_network = cls.env.ref('KSW_helpdesk.ticket_category_network')
        cls.cat_password = cls.env.ref('KSW_helpdesk.ticket_category_password_reset')

        cls.asset_category = cls.env['it.asset.category'].create({
            'name': 'Test Laptops',
            'sequence': 10,
        })

        Users = cls.env['res.users']
        cls.user_manager = Users.create({
            'name': 'Mona Manager',
            'login': 'ksw_hd_manager',
            'email': 'mona.manager@example.com',
            'group_ids': [(6, 0, [cls.internal_group.id])],
        })
        cls.user_employee = Users.create({
            'name': 'Emad Employee',
            'login': 'ksw_hd_employee',
            'email': 'emad.employee@example.com',
            'group_ids': [(6, 0, [cls.internal_group.id])],
        })
        cls.user_stranger = Users.create({
            'name': 'Sami Stranger',
            'login': 'ksw_hd_stranger',
            'email': 'sami.stranger@example.com',
            'group_ids': [(6, 0, [cls.internal_group.id])],
        })
        cls.user_agent = Users.create({
            'name': 'Adel Agent',
            'login': 'ksw_hd_agent',
            'email': 'adel.agent@example.com',
            'group_ids': [(6, 0, [cls.internal_group.id, cls.agent_group.id])],
        })
        cls.user_agent2 = Users.create({
            'name': 'Basel Agent',
            'login': 'ksw_hd_agent2',
            'email': 'basel.agent@example.com',
            'group_ids': [(6, 0, [cls.internal_group.id, cls.agent_group.id])],
        })

        Employee = cls.env['hr.employee']
        cls.emp_manager = Employee.create({
            'name': 'Mona Manager',
            'user_id': cls.user_manager.id,
            'work_email': 'mona.manager@example.com',
        })
        cls.emp_employee = Employee.create({
            'name': 'Emad Employee',
            'user_id': cls.user_employee.id,
            'parent_id': cls.emp_manager.id,
            'work_email': 'emad.employee@example.com',
        })
        cls.emp_stranger = Employee.create({
            'name': 'Sami Stranger',
            'user_id': cls.user_stranger.id,
            'work_email': 'sami.stranger@example.com',
        })
        cls.emp_agent = Employee.create({
            'name': 'Adel Agent',
            'user_id': cls.user_agent.id,
            'work_email': 'adel.agent@example.com',
        })
        # No Odoo login at all - the "requester has no user account" path.
        cls.emp_no_user = Employee.create({
            'name': 'Nadia NoLogin',
            'work_email': 'nadia.nologin@example.com',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _ticket_vals(self, **overrides):
        vals = {
            'name': 'Laptop will not boot',
            'description': '<p>Black screen since this morning.</p>',
            'ticket_type': 'incident',
            'category_id': self.cat_hardware.id,
            'employee_id': self.emp_employee.id,
        }
        vals.update(overrides)
        return vals

    def _new_ticket(self, user=None, **overrides):
        model = self.env['helpdesk.ticket']
        if user is not None:
            model = model.with_user(user)
        return model.create(self._ticket_vals(**overrides))

    def _new_asset(self, **overrides):
        vals = {
            'name': 'Dell Latitude 5420',
            'category_id': self.asset_category.id,
            'serial_number': 'SN-TEST-0001',
        }
        vals.update(overrides)
        return self.env['it.asset'].create(vals)

    @staticmethod
    def _messages_after(ticket, known_ids):
        """New chatter messages only (CLAUDE.md gotcha #23)."""
        return ticket.message_ids.filtered(lambda m: m.id not in known_ids)
