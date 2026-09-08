from odoo.tests.common import tagged

from .common import HelpdeskCommon


@tagged('post_install', '-at_install')
class TestHelpdeskNotifications(HelpdeskCommon):
    """Who gets told what, and when.

    Assertions use assertIn/assertNotIn on a message's partner_ids rather
    than equality: a live database has IT Team members of its own, and the
    fixture's agents are only ever a subset of the real recipients.
    """

    def setUp(self):
        super().setUp()
        self.partner_agent = self.user_agent.partner_id
        self.partner_agent2 = self.user_agent2.partner_id
        self.partner_employee = self.user_employee.partner_id

    @staticmethod
    def _notified_partners(messages):
        return messages.mapped('partner_ids')

    # ------------------------------------------------------------------
    # New ticket -> the whole IT Team
    # ------------------------------------------------------------------
    def test_new_ticket_notifies_every_it_team_member(self):
        ticket = self._new_ticket(user=self.user_employee)
        notified = self._notified_partners(ticket.message_ids)
        self.assertIn(self.partner_agent, notified)
        self.assertIn(self.partner_agent2, notified)

    def test_new_ticket_notification_is_a_comment_not_a_note(self):
        """mt_note would never reach an inbox."""
        ticket = self._new_ticket(user=self.user_employee)
        comment = self.env.ref('mail.mt_comment')
        addressed = ticket.message_ids.filtered(
            lambda m: self.partner_agent in m.partner_ids)
        self.assertTrue(addressed)
        self.assertTrue(all(m.subtype_id == comment for m in addressed))

    def test_new_ticket_notification_names_the_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        body = ' '.join(ticket.message_ids.filtered(
            lambda m: self.partner_agent in m.partner_ids).mapped('body'))
        self.assertIn(ticket.ticket_ref, body)
        self.assertIn('Laptop will not boot', body)
        self.assertIn(self.emp_employee.name, body)

    def test_an_agent_filing_a_ticket_is_not_notified_of_their_own(self):
        ticket = self._new_ticket(user=self.user_agent, employee_id=self.emp_agent.id)
        notified = self._notified_partners(ticket.message_ids)
        self.assertNotIn(self.partner_agent, notified)
        self.assertIn(self.partner_agent2, notified)

    def test_requester_is_not_in_the_new_ticket_recipients(self):
        """They follow the thread instead - no duplicate ping."""
        ticket = self._new_ticket(user=self.user_employee)
        new_ticket_msgs = ticket.message_ids.filtered(
            lambda m: self.partner_agent in m.partner_ids)
        self.assertNotIn(self.partner_employee, new_ticket_msgs.mapped('partner_ids'))

    def test_requester_follows_their_own_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        self.assertIn(self.partner_employee, ticket.message_partner_ids)

    def test_requester_without_a_login_still_follows_via_work_contact(self):
        ticket = self._new_ticket(
            user=self.user_agent, employee_id=self.emp_no_user.id)
        self.assertTrue(self.emp_no_user.work_contact_id)
        self.assertIn(self.emp_no_user.work_contact_id, ticket.message_partner_ids)

    def test_helpdesk_agent_partners_excludes_the_acting_user(self):
        Ticket = self.env['helpdesk.ticket'].with_user(self.user_agent)
        partners = Ticket._helpdesk_agent_partners()
        self.assertNotIn(self.partner_agent, partners)
        self.assertIn(self.partner_agent2, partners)

    def test_helpdesk_agent_partners_excludes_odoobot(self):
        partners = self.env['helpdesk.ticket']._helpdesk_agent_partners()
        self.assertNotIn(self.env.ref('base.user_root').partner_id, partners)

    def test_helpdesk_agent_partners_excludes_an_archived_agent(self):
        self.user_agent2.active = False
        partners = self.env['helpdesk.ticket']._helpdesk_agent_partners()
        self.assertNotIn(self.partner_agent2, partners)

    # ------------------------------------------------------------------
    # Assignment -> the assignee
    # ------------------------------------------------------------------
    def test_assigning_notifies_the_new_assignee(self):
        ticket = self._new_ticket(user=self.user_employee)
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).write({'user_id': self.user_agent2.id})
        new_msgs = self._messages_after(ticket, known)
        addressed = new_msgs.filtered(lambda m: self.partner_agent2 in m.partner_ids)
        self.assertTrue(addressed, 'the new assignee was not notified')
        self.assertIn(ticket.ticket_ref, ' '.join(addressed.mapped('body')))

    def test_assignee_becomes_a_follower(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).write({'user_id': self.user_agent2.id})
        self.assertIn(self.partner_agent2, ticket.message_partner_ids)

    def test_assign_to_me_does_not_notify_me(self):
        ticket = self._new_ticket(user=self.user_employee)
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).action_assign_to_me()
        new_msgs = self._messages_after(ticket, known)
        self.assertFalse(new_msgs.filtered(lambda m: self.partner_agent in m.partner_ids))
        self.assertIn(self.partner_agent, ticket.message_partner_ids)

    def test_re_saving_the_same_assignee_notifies_nobody(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).write({'user_id': self.user_agent2.id})
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).write({'user_id': self.user_agent2.id})
        new_msgs = self._messages_after(ticket, known)
        self.assertFalse(new_msgs.filtered(lambda m: self.partner_agent2 in m.partner_ids))

    def test_assignment_at_creation_notifies_the_assignee(self):
        ticket = self._new_ticket(user=self.user_agent, user_id=self.user_agent2.id)
        addressed = ticket.message_ids.filtered(
            lambda m: self.partner_agent2 in m.partner_ids)
        self.assertTrue(addressed)

    # ------------------------------------------------------------------
    # Close -> the person who raised the ticket
    # ------------------------------------------------------------------
    def test_closing_notifies_the_requester(self):
        ticket = self._new_ticket(user=self.user_employee)
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).action_close()
        new_msgs = self._messages_after(ticket, known)
        addressed = new_msgs.filtered(lambda m: self.partner_employee in m.partner_ids)
        self.assertTrue(addressed, 'the requester was not told their ticket closed')
        body = ' '.join(addressed.mapped('body'))
        self.assertIn(ticket.ticket_ref, body)
        self.assertIn(self.user_agent.name, body)
        self.assertEqual(addressed.subtype_id, self.env.ref('mail.mt_comment'))

    def test_closing_by_stage_change_notifies_the_requester_too(self):
        """Dragging the card into the Closed column is the common route."""
        ticket = self._new_ticket(user=self.user_employee)
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).write({'stage_id': self.stage_closed.id})
        new_msgs = self._messages_after(ticket, known)
        self.assertTrue(
            new_msgs.filtered(lambda m: self.partner_employee in m.partner_ids))

    def test_a_write_on_an_already_closed_ticket_does_not_notify_again(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).action_close()
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).write({'kanban_state': 'done'})
        ticket.with_user(self.user_agent).write({'stage_id': self.stage_closed.id})
        new_msgs = self._messages_after(ticket, known)
        self.assertFalse(
            new_msgs.filtered(lambda m: self.partner_employee in m.partner_ids))

    def test_closing_again_after_a_reopen_notifies_again(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).action_close()
        ticket.with_user(self.user_agent).action_reopen()
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).action_close()
        new_msgs = self._messages_after(ticket, known)
        self.assertTrue(
            new_msgs.filtered(lambda m: self.partner_employee in m.partner_ids))

    def test_reopening_notifies_nobody(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_agent).action_close()
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent).action_reopen()
        new_msgs = self._messages_after(ticket, known)
        self.assertFalse(new_msgs.mapped('partner_ids'))

    def test_close_reaches_a_requester_who_has_no_odoo_login(self):
        ticket = self._new_ticket(
            user=self.user_agent, employee_id=self.emp_no_user.id)
        known = ticket.message_ids.ids
        ticket.with_user(self.user_agent2).action_close()
        new_msgs = self._messages_after(ticket, known)
        self.assertTrue(new_msgs.filtered(
            lambda m: self.emp_no_user.work_contact_id in m.partner_ids))

    def test_close_falls_back_to_the_email_template_without_any_partner(self):
        """hr.employee._remove_work_contact_id() can leave an employee with
        no partner at all; the branded template still reaches work_email."""
        self.emp_no_user.work_contact_id = False
        ticket = self._new_ticket(
            user=self.user_agent, employee_id=self.emp_no_user.id)
        self.assertFalse(ticket._requester_partner())
        before = self.env['mail.mail'].search([])
        ticket.with_user(self.user_agent2).action_close()
        sent = self.env['mail.mail'].search([]) - before
        self.assertTrue(sent, 'no fallback email was queued')
        self.assertIn('nadia.nologin@example.com', ' '.join(sent.mapped('email_to')))
        self.assertIn(ticket.ticket_ref, ' '.join(sent.mapped('subject')))

    def test_closed_email_template_renders(self):
        template = self.env.ref('KSW_helpdesk.mail_template_ticket_closed')
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).action_close()
        rendered = template._render_field('body_html', ticket.ids)[ticket.id]
        self.assertIn(ticket.ticket_ref, rendered)
        self.assertIn(self.user_agent.name, rendered)

    # ------------------------------------------------------------------
    # A missing closing stage must not fail silently
    # ------------------------------------------------------------------
    def test_close_without_a_closing_stage_raises(self):
        from odoo.exceptions import UserError
        self.env['helpdesk.ticket.stage'].search(
            [('is_closed', '=', True)]).write({'is_closed': False})
        ticket = self._new_ticket()
        with self.assertRaises(UserError):
            ticket.with_user(self.user_agent).action_close()
