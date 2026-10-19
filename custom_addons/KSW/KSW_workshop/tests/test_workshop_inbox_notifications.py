from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkshopInboxNotifications(TransactionCase):
    """Workshop Notifications in Odoo Only moves workshop mail to the inbox
    for that user alone, and leaves every other app on his email setting."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, groups, **vals):
            user = Users.create({
                'name': login, 'login': login, 'email': f'{login}@inbox.test',
                'notification_type': 'email',
                'group_ids': [(6, 0, [cls.env.ref(g).id for g in groups])], **vals})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        manager_group = ('KSW_workshop.group_workshop_manager',)
        cls.inbox_manager = _user('ibx_manager', manager_group, x_workshop_notify_in_odoo=True)
        cls.email_manager = _user('ibx_manager_mail', manager_group)
        cls.requester = _user('ibx_requester', ('KSW_workshop.group_workshop_requester',))
        cls.vehicle = cls.env['ksw.fleet.vehicle'].create({
            'name': 'IBX1', 'vehicle_type': 'isuzu', 'driver_id': cls.requester.employee_id.id})

    def _notification_type(self, message, user):
        notif = message.notification_ids.filtered(lambda n: n.res_partner_id == user.partner_id)
        self.assertEqual(len(notif), 1, user.login)
        return notif.notification_type

    def test_new_request_goes_to_inbox_only_for_the_opted_in_manager(self):
        request = self.env['ksw.workshop.request'].with_user(self.requester).create({
            'vehicle_type': 'isuzu', 'vehicle_id': self.vehicle.id,
            'driver_id': self.requester.employee_id.id, 'description': 'Oil change'})
        message = request.message_ids.filtered(lambda m: 'New workshop request' in (m.body or ''))
        self.assertEqual(self._notification_type(message, self.inbox_manager), 'inbox')
        self.assertEqual(self._notification_type(message, self.email_manager), 'email')
        # His global setting is untouched.
        self.assertEqual(self.inbox_manager.notification_type, 'email')

    def test_other_apps_still_email_him(self):
        partner = self.env['res.partner'].create({'name': 'Elsewhere'})
        message = partner.message_post(
            body='Not a workshop message', partner_ids=self.inbox_manager.partner_id.ids,
            subtype_xmlid='mail.mt_comment')
        self.assertEqual(self._notification_type(message, self.inbox_manager), 'email')

    def test_user_can_set_it_himself(self):
        self.email_manager.with_user(self.email_manager).write({'x_workshop_notify_in_odoo': True})
        self.assertTrue(self.email_manager.x_workshop_notify_in_odoo)
