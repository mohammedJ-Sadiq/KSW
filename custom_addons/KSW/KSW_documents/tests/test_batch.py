from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

# What Claude answered for the sample scans (Nov 2026), as _parse_all returns it.
ISTIMARA_1 = {'type_code': 'istimara', 'number': '596812347', 'plate': 'ق ك ل ٩٩٠١',
              'vin': 'TSTB634031C0TEST1', 'make': 'Mercedes', 'model': 'Actros', 'year': 2020,
              'color': 'White'}
CARD_1 = {'type_code': 'operation_card', 'number': '11-00452625', 'expiry_date': '2027-01-05',
          'plate': '9901 L K G'}
ABSHER = [{'type_code': 'iqama', 'number': '2999999992', 'expiry_date': '2027-01-08',
           'holder_name': 'ANAS KHAN', 'id_number': '2999999992', 'nationality': 'Pakistan'},
          {'type_code': 'driving_licence', 'number': '2999999992', 'license_class': 'Heavy Transport',
           'expiry_date': '2031-04-20', 'expiry_date_as_printed': '20/04/2031',
           'holder_name': 'ANAS KHAN', 'id_number': '2999999992'}]
COMPANY = {'type_code': 'company_transport_licence', 'number': '11/99999384', 'expiry_date': '2028-01-13',
           'id_number': '7999999101', 'holder_name': 'kawthar aladhb Company For Transportation'}


@tagged('post_install', '-at_install')
class TestDocumentBatch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.officer = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'BATCH Officer', 'login': 'batch_officer', 'group_ids': [(6, 0, [
                cls.env.ref('KSW_documents.group_document_officer').id, cls.env.ref('hr.group_hr_user').id])]})
        # The master data is as thin as on the real database: the plate in
        # Latin letters, no chassis number, no make.
        cls.head = cls.env['ksw.fleet.vehicle'].create({'name': 'T-BATCH', 'vehicle_type': 'trailer',
                                                        'plate_number': 'LKG 9901'})
        cls.anas = cls.env['hr.employee'].create({'name': 'Anas Khan'})
        cls.types = cls.env['ksw.document.type'].search([])

    def _batch(self, answers):
        """A batch of one scan per answer (a list = several documents on one scan)."""
        Attachment = self.env['ir.attachment'].with_user(self.officer)
        scans = Attachment.browse()
        by_scan = {}
        for i, answer in enumerate(answers):
            scan = Attachment.create({'name': f'scan{i}.png', 'raw': b'\x89PNG fake', 'mimetype': 'image/png'})
            scans |= scan
            by_scan[scan.id] = answer if isinstance(answer, list) else [answer]
        Reader = self.env.registry['ksw.document.ai.reader']

        def fake_read_all(reader, attachment, types):
            return [reader._parse_one(dict(d), types) for d in by_scan[attachment.id]]

        batch = self.env['ksw.document.batch'].with_user(self.officer).create(
            {'attachment_ids': [(6, 0, scans.ids)]})
        with patch.object(Reader, '_is_available', lambda self: True), \
                patch.object(Reader, '_read_all', fake_read_all):
            batch.action_read()
        return batch

    def _of(self, batch, code):
        return batch.document_ids.filtered(lambda d: d.type_id.code == code)

    def test_plate_in_arabic_finds_vehicle_and_fills_master(self):
        batch = self._batch([ISTIMARA_1, CARD_1])
        istimara, card = self._of(batch, 'istimara'), self._of(batch, 'operation_card')
        self.assertEqual(istimara.vehicle_id, self.head)
        self.assertEqual(card.vehicle_id, self.head)
        self.assertEqual(istimara.match_note, 'Plate')
        self.assertFalse(batch.unmatched_count)
        batch.action_confirm_all()
        self.assertEqual(istimara.state, 'confirmed', batch.errors)
        self.assertEqual(card.state, 'confirmed', batch.errors)
        # The istimara prints no expiry: confirmed without one, shown as unknown.
        self.assertFalse(istimara.expiry_date)
        self.assertEqual(istimara.expiry_status, 'unknown')
        # It filled what the vehicle lacked; the plate (same, other script) is untouched.
        self.assertEqual(self.head.x_vin, 'TSTB634031C0TEST1')
        self.assertEqual((self.head.brand, self.head.year), ('Mercedes', 2020))
        # Kept, and stored in both scripts.
        self.assertEqual((self.head.plate_number, self.head.x_plate_en), ('ق ك ل 9901', 'LKG 9901'))
        self.assertFalse(istimara.message_ids.filtered(lambda m: 'Differs' in (m.body or '')))

    def test_chassis_wins_and_ties_the_plate_group(self):
        self.head.x_vin = 'TSTB 634031C0TEST1'
        self.head.plate_number = False
        batch = self._batch([ISTIMARA_1, CARD_1])
        self.assertEqual(self._of(batch, 'istimara').match_note, 'Chassis number')
        # The card only shows the plate: it follows the istimara of the same plate.
        self.assertEqual(self._of(batch, 'operation_card').vehicle_id, self.head)

    def test_unknown_owner_hr_chooses_once_group_follows(self):
        self.anas.name = 'Somebody Else'
        batch = self._batch([ABSHER])
        iqama, licence = self._of(batch, 'iqama'), self._of(batch, 'driving_licence')
        self.assertFalse(iqama.employee_id or licence.employee_id)
        self.assertEqual(batch.unmatched_count, 2)
        iqama.with_user(self.officer).employee_id = self.anas
        self.assertEqual(licence.employee_id, self.anas)
        self.assertFalse(batch.unmatched_count)
        self.assertIn('20/04/2031', licence.ai_notes)
        batch.action_confirm_all()
        self.assertEqual(self.anas.ssnid, '2999999992')
        self.assertEqual(self.anas.country_id, self.env.ref('base.pk'))
        self.assertEqual(licence.license_class, 'Heavy Transport')

    def test_identified_employee_is_found_next_time(self):
        # Payroll already has his iqama number in SSN No. (WPS file).
        self.anas.ssnid = '2999999992'
        self.anas.name = 'A. Khan'
        batch = self._batch([ABSHER])
        self.assertEqual(self._of(batch, 'iqama').employee_id, self.anas)
        self.assertEqual(self._of(batch, 'iqama').match_note, 'Iqama / ID number')

    def test_company_document_goes_to_its_legal_entity(self):
        # Issued to another entity of the group: never put on the current company.
        batch = self._batch([COMPANY])
        doc = self._of(batch, 'company_transport_licence')
        self.assertFalse(doc.company_id)
        self.assertIn('7999999101', doc.ai_notes)
        # Once the branch is registered, its documents are found by its number.
        transport = self.env['ksw.company.registration'].create({
            'name': 'Kawther Al-Adhb Transport', 'kind': 'branch', 'unified_number': '7999999101',
            'cr_number': '2099999901'})
        batch = self._batch([COMPANY])
        doc = self._of(batch, 'company_transport_licence')
        self.assertEqual(doc.company_id, self.env.company)
        self.assertEqual(doc.registration_id, transport)
        batch.action_confirm_all()
        self.assertEqual(doc.state, 'confirmed', batch.errors)
        # The licence on the next operation card is already in the register.
        again = self._batch([COMPANY])
        self.assertFalse(again.document_ids)
        self.assertIn('11/99999384', again.info)

    def test_licence_printed_on_two_cards_is_listed_once(self):
        batch = self._batch([[CARD_1, COMPANY], [dict(CARD_1, number='11-00605498'), COMPANY]])
        self.assertEqual(len(self._of(batch, 'company_transport_licence')), 1)
        self.assertEqual(len(self._of(batch, 'company_transport_licence').attachment_ids), 2)
        self.assertEqual(len(self._of(batch, 'operation_card')), 2)

    def test_plate_written_in_both_scripts(self):
        batch = self._batch([dict(ISTIMARA_1, plate='9901 LKG / ق ك ل ٩٩٠١', vin=False), CARD_1])
        self.assertEqual(self._of(batch, 'istimara').vehicle_id, self.head)
        self.assertEqual(self._of(batch, 'operation_card').vehicle_id, self.head)

    def test_failed_file_does_not_stop_the_others(self):
        Reader = self.env.registry['ksw.document.ai.reader']
        original = Reader._parse_one

        def broken(reader, data, types):
            if data.get('type_code') == 'iqama':
                from odoo.exceptions import UserError
                raise UserError('unreadable')
            return original(reader, data, types)

        with patch.object(Reader, '_parse_one', broken):
            batch = self._batch([ABSHER, COMPANY])
        self.assertIn('unreadable', batch.errors)
        self.assertEqual(len(batch.document_ids), 1)
        self.assertEqual(batch.unread_count, 1)

    def test_cr_certificates_build_the_registrations(self):
        Registration = self.env['ksw.company.registration']
        main_cr = {'type_code': 'commercial_registration', 'number': '7999999100', 'id_number': '7999999100',
                   'holder_name': 'شركة كوثر المياه العذبة للمقاولات', 'issue_date': '1989-10-21'}
        # Unknown to Odoo yet: HR says whose it is once, confirming records it.
        batch = self._batch([main_cr])
        doc = self._of(batch, 'commercial_registration')
        self.assertFalse(doc.company_id)
        doc.with_user(self.officer).company_id = self.env.company
        batch.action_confirm_all()
        main = Registration.search([('unified_number', '=', '7999999100')])
        self.assertEqual((main.company_id, doc.registration_id), (self.env.company, main))
        # Main unless the company already has one (then HR says which it is).
        has_main = Registration.search_count([('kind', '=', 'main'), ('id', '!=', main.id)])
        self.assertEqual(main.kind, 'branch' if has_main else 'main')
        main.write({'kind': 'main', 'cr_number': '2099999900'})
        # A branch certificate names its main CR: it finds the company alone.
        transport_cr = {'type_code': 'commercial_registration', 'number': '2099999901',
                        'id_number': '٧٩٩٩٩٩٩١٠١', 'parent_cr_number': '2099999900',
                        'holder_name': 'شركة كوثر العذب للنقليات'}
        workshop_cr = dict(transport_cr, number='2099999902', id_number='7999999102',
                           holder_name='شركة كوثر المياه العذبة لمكينيكا السيارات')
        batch = self._batch([transport_cr, workshop_cr])
        self.assertEqual(batch.document_ids.company_id, self.env.company)
        batch.action_confirm_all()
        self.assertEqual(set(batch.document_ids.mapped('state')), {'confirmed'}, batch.errors)
        transport = Registration.search([('unified_number', '=', '7999999101')])
        self.assertEqual((transport.kind, transport.parent_id, transport.cr_number), ('branch', main, '2099999901'))
        # Two branches' certificates do not replace each other.
        self.assertEqual(len(batch.document_ids.mapped('registration_id')), 2)
        # The transport licence now lands on the transport branch by itself.
        batch = self._batch([COMPANY])
        self.assertEqual(self._of(batch, 'company_transport_licence').registration_id, transport)

    def test_company_document_needs_its_registration(self):
        from odoo.exceptions import UserError
        doc = self.env['ksw.document'].with_user(self.officer).create({
            'type_id': self.env.ref('KSW_documents.type_company_transport_licence').id, 'applies_to': 'company',
            'company_id': self.env.company.id, 'number': '1', 'expiry_date': '2030-01-01',
            'attachment_ids': [(6, 0, self.env['ir.attachment'].with_user(self.officer).create(
                {'name': 's.png', 'raw': b'x', 'mimetype': 'image/png'}).ids)]})
        with self.assertRaises(UserError):
            doc.action_confirm()

    def test_iqama_number_payroll_uses_is_never_overwritten(self):
        self.anas.ssnid = '2999999001'
        batch = self._batch([ABSHER])
        iqama = self._of(batch, 'iqama')
        iqama.with_user(self.officer).employee_id = self.anas
        batch.action_confirm_all()
        self.assertEqual(self.anas.ssnid, '2999999001')
        self.assertTrue(iqama.message_ids.filtered(lambda m: '2999999992' in (m.body or '')))

    def test_match_again_after_master_data_arrives(self):
        self.anas.name = 'Somebody Else'
        batch = self._batch([ABSHER])
        self.assertEqual(batch.unmatched_count, 2)
        self.anas.ssnid = '2999999992'
        batch.with_user(self.officer).action_match_again()
        self.assertEqual(batch.document_ids.employee_id, self.anas)
        self.assertFalse(batch.unmatched_count)
