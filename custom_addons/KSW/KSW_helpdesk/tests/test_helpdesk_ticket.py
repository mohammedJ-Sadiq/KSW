import base64
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import HelpdeskCommon


@tagged('post_install', '-at_install')
class TestHelpdeskTicket(HelpdeskCommon):
    """End-to-end behaviour of a ticket: numbering, defaults, guards, search."""

    # ------------------------------------------------------------------
    # Numbering & defaults
    # ------------------------------------------------------------------
    def test_incident_takes_the_incident_sequence(self):
        ticket = self._new_ticket(ticket_type='incident')
        self.assertTrue(ticket.ticket_ref.startswith('INC'), ticket.ticket_ref)
        self.assertNotEqual(ticket.ticket_ref, 'New')

    def test_request_takes_the_request_sequence(self):
        ticket = self._new_ticket(
            ticket_type='request', category_id=self.cat_password.id,
        )
        self.assertTrue(ticket.ticket_ref.startswith('REQ'), ticket.ticket_ref)

    def test_two_tickets_never_share_a_reference(self):
        first = self._new_ticket()
        second = self._new_ticket()
        self.assertNotEqual(first.ticket_ref, second.ticket_ref)

    def test_new_ticket_lands_in_the_first_stage(self):
        ticket = self._new_ticket()
        self.assertEqual(ticket.stage_id, self.stage_new)
        self.assertFalse(ticket.is_closed)
        self.assertEqual(ticket.kanban_state, 'normal')

    # ------------------------------------------------------------------
    # Category / type coherence
    # ------------------------------------------------------------------
    def test_category_must_belong_to_the_ticket_type(self):
        with self.assertRaises(ValidationError):
            self._new_ticket(
                ticket_type='incident', category_id=self.cat_password.id,
            )

    def test_changing_type_to_a_mismatching_category_is_refused(self):
        ticket = self._new_ticket()
        with self.assertRaises(ValidationError):
            ticket.ticket_type = 'request'

    def test_onchange_clears_a_category_of_the_wrong_type(self):
        form = self.env['helpdesk.ticket'].new(self._ticket_vals())
        self.assertEqual(form.category_id, self.cat_hardware)
        form.ticket_type = 'request'
        form._onchange_ticket_type()
        self.assertFalse(form.category_id)

    # ------------------------------------------------------------------
    # Caller card - a sudo compute, so a non-HR agent can read it
    # ------------------------------------------------------------------
    def test_caller_card_mirrors_the_employee(self):
        self.emp_employee.write({
            'work_phone': '+966 11 000 0000',
            'job_title': 'Accountant',
        })
        ticket = self._new_ticket()
        self.assertEqual(ticket.caller_email, 'emad.employee@example.com')
        self.assertEqual(ticket.caller_work_phone, '+966 11 000 0000')
        self.assertEqual(ticket.caller_job_title, 'Accountant')

    def test_caller_card_readable_by_an_agent_who_is_not_hr(self):
        ticket = self._new_ticket()
        self.assertFalse(self.user_agent.has_group('hr.group_hr_user'))
        as_agent = ticket.with_user(self.user_agent)
        as_agent.invalidate_recordset()
        self.assertEqual(as_agent.caller_email, 'emad.employee@example.com')

    # ------------------------------------------------------------------
    # Overdue: compute + the search operator triage (gotcha #24)
    # ------------------------------------------------------------------
    def test_overdue_only_while_the_ticket_is_open(self):
        yesterday = fields.Date.context_today(self.env['helpdesk.ticket']) - timedelta(days=1)
        ticket = self._new_ticket(deadline=yesterday)
        self.assertTrue(ticket.is_overdue)
        ticket.with_user(self.user_agent).action_close()
        self.assertFalse(ticket.is_overdue)

    def test_overdue_search_does_not_match_everything(self):
        today = fields.Date.context_today(self.env['helpdesk.ticket'])
        late = self._new_ticket(deadline=today - timedelta(days=1))
        on_time = self._new_ticket(deadline=today + timedelta(days=7))
        Ticket = self.env['helpdesk.ticket']

        overdue = Ticket.search([('id', 'in', (late | on_time).ids), ('is_overdue', '=', True)])
        self.assertEqual(overdue, late)

        not_overdue = Ticket.search([('id', 'in', (late | on_time).ids), ('is_overdue', '=', False)])
        self.assertEqual(not_overdue, on_time)

        negated = Ticket.search([('id', 'in', (late | on_time).ids), ('is_overdue', '!=', True)])
        self.assertEqual(negated, on_time)

    def test_overdue_search_rejects_an_unsupported_operator(self):
        self.assertIs(
            self.env['helpdesk.ticket']._search_is_overdue('like', True),
            NotImplemented,
        )

    # ------------------------------------------------------------------
    # Resolution time
    # ------------------------------------------------------------------
    def test_resolution_hours_only_once_closed(self):
        ticket = self._new_ticket()
        self.assertEqual(ticket.resolution_hours, 0.0)
        ticket.with_user(self.user_agent).action_close()
        self.assertGreaterEqual(ticket.resolution_hours, 0.0)
        self.assertTrue(ticket.close_date)

    # ------------------------------------------------------------------
    # Who may report for whom
    # ------------------------------------------------------------------
    def test_employee_reports_for_themselves(self):
        ticket = self._new_ticket(user=self.user_employee)
        self.assertEqual(ticket.employee_id, self.emp_employee)

    def test_manager_reports_for_a_direct_report(self):
        ticket = self._new_ticket(user=self.user_manager)
        self.assertEqual(ticket.employee_id, self.emp_employee)

    def test_employee_cannot_report_for_a_stranger(self):
        with self.assertRaises(UserError):
            self._new_ticket(user=self.user_employee, employee_id=self.emp_stranger.id)

    def test_employee_cannot_reassign_the_reporter_to_a_stranger(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).write(
                {'employee_id': self.emp_stranger.id})

    def test_agent_may_report_for_anyone(self):
        ticket = self._new_ticket(user=self.user_agent, employee_id=self.emp_stranger.id)
        self.assertEqual(ticket.employee_id, self.emp_stranger)

    def test_reporter_domain_narrows_for_a_plain_employee(self):
        Ticket = self.env['helpdesk.ticket'].with_user(self.user_employee)
        domain = Ticket._domain_employee_id()
        self.assertTrue(domain, 'a plain employee must not get an open domain')
        allowed = self.env['hr.employee'].sudo().search(domain)
        self.assertIn(self.emp_employee, allowed)
        self.assertNotIn(self.emp_stranger, allowed)

        as_agent = self.env['helpdesk.ticket'].with_user(self.user_agent)
        self.assertEqual(as_agent._domain_employee_id(), [])

    # ------------------------------------------------------------------
    # Status fields are IT Team only, over RPC too (gotcha #15)
    # ------------------------------------------------------------------
    def test_employee_cannot_move_the_stage(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).write(
                {'stage_id': self.stage_in_progress.id})

    def test_employee_cannot_assign_a_ticket(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).write({'user_id': self.user_agent.id})

    def test_employee_cannot_flip_the_kanban_state(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).write({'kanban_state': 'done'})

    def test_employee_may_still_edit_their_own_description(self):
        ticket = self._new_ticket(user=self.user_employee)
        ticket.with_user(self.user_employee).write({'name': 'Laptop is dead'})
        self.assertEqual(ticket.name, 'Laptop is dead')

    def test_agent_moves_the_stage(self):
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).write({'stage_id': self.stage_in_progress.id})
        self.assertEqual(ticket.stage_id, self.stage_in_progress)

    def test_employee_cannot_close_or_reopen(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).action_close()
        ticket.with_user(self.user_agent).action_close()
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).action_reopen()

    def test_employee_cannot_assign_the_ticket_to_themselves(self):
        ticket = self._new_ticket(user=self.user_employee)
        with self.assertRaises(UserError):
            ticket.with_user(self.user_employee).action_assign_to_me()

    # ------------------------------------------------------------------
    # Assignee must be an IT Team member
    # ------------------------------------------------------------------
    def test_assignee_must_be_an_agent(self):
        ticket = self._new_ticket()
        with self.assertRaises(UserError):
            ticket.with_user(self.user_agent).write({'user_id': self.user_employee.id})

    def test_assignee_refused_at_creation_too(self):
        with self.assertRaises(UserError):
            self._new_ticket(user=self.user_agent, user_id=self.user_stranger.id)

    def test_assign_to_me_sets_the_agent(self):
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).action_assign_to_me()
        self.assertEqual(ticket.user_id, self.user_agent)

    # ------------------------------------------------------------------
    # Close / reopen lifecycle
    # ------------------------------------------------------------------
    def test_close_stamps_who_and_when(self):
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).action_close()
        self.assertTrue(ticket.is_closed)
        self.assertEqual(ticket.stage_id, self.stage_closed)
        self.assertEqual(ticket.closed_by, self.user_agent)
        self.assertEqual(ticket.kanban_state, 'done')

    def test_dragging_the_card_into_the_closed_column_closes_it_too(self):
        """The Close button is not the only route into the closed state."""
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).write({'stage_id': self.stage_closed.id})
        self.assertTrue(ticket.is_closed)
        self.assertTrue(ticket.close_date)
        self.assertEqual(ticket.closed_by, self.user_agent)

    def test_reopen_clears_the_close_stamps(self):
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).action_close()
        ticket.with_user(self.user_agent).action_reopen()
        self.assertFalse(ticket.is_closed)
        self.assertFalse(ticket.close_date)
        self.assertFalse(ticket.closed_by)
        self.assertEqual(ticket.stage_id, self.stage_new)
        self.assertEqual(ticket.resolution_hours, 0.0)

    def test_close_stamp_is_not_overwritten_by_a_later_edit(self):
        ticket = self._new_ticket()
        ticket.with_user(self.user_agent).action_close()
        stamped_at, stamped_by = ticket.close_date, ticket.closed_by
        ticket.with_user(self.user_agent2).write({'satisfaction': 'great'})
        self.assertEqual(ticket.close_date, stamped_at)
        self.assertEqual(ticket.closed_by, stamped_by)

    # ------------------------------------------------------------------
    # Attachments uploaded before the first save
    # ------------------------------------------------------------------
    def test_attachment_uploaded_before_save_is_relinked(self):
        orphan = self.env['ir.attachment'].create({
            'name': 'screenshot.png',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'helpdesk.ticket',
            'res_id': 0,
        })
        ticket = self._new_ticket(attachment_ids=[(6, 0, orphan.ids)])
        self.assertEqual(orphan.res_id, ticket.id)
        self.assertEqual(orphan.res_model, 'helpdesk.ticket')

    # ------------------------------------------------------------------
    # Kanban grouping shows every stage, empty ones included
    # ------------------------------------------------------------------
    def test_stage_group_expand_returns_every_stage_in_order(self):
        Stage = self.env['helpdesk.ticket.stage']
        expanded = self.env['helpdesk.ticket']._read_group_stage_ids(Stage, [])
        self.assertEqual(expanded, Stage.search([], order='sequence'))
        self.assertIn(self.stage_closed, expanded)

    # ------------------------------------------------------------------
    # Category configuration
    # ------------------------------------------------------------------
    @mute_logger('odoo.sql_db')
    def test_same_category_name_allowed_once_per_ticket_type(self):
        Category = self.env['helpdesk.ticket.category']
        incident = Category.create({'name': 'VPN', 'ticket_type': 'incident'})
        request = Category.create({'name': 'VPN', 'ticket_type': 'request'})
        self.assertNotEqual(incident, request)
        with self.assertRaises(IntegrityError), self.cr.savepoint(flush=False):
            Category.create({'name': 'VPN', 'ticket_type': 'incident'}).flush_recordset()
