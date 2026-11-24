from odoo import _, api, fields, models


class KswDocumentRequirement(models.AbstractModel):
    """What a vehicle or an employee must hold, and what it lacks: a required
    type with no current document, or whose current one has expired."""
    _name = 'ksw.document.owner.mixin'
    _description = 'Document Owner'

    x_documents_missing = fields.Char(string='Missing or Expired Documents',
                                      compute='_compute_documents_missing', compute_sudo=True)
    x_document_issue = fields.Boolean(string='Documents Missing or Expired',
                                      compute='_compute_documents_missing', compute_sudo=True,
                                      search='_search_document_issue')
    # Home ▸ Waiting for you: only the documents officer is asked to fill
    # them in; everyone else reads vehicles without this being theirs.
    x_document_pending_my_action = fields.Boolean(compute='_compute_document_pending',
                                                  search='_search_document_pending',
                                                  string='Documents Waiting for Me')

    @api.depends_context('uid')
    def _compute_document_pending(self):
        mine = self.env.su or self.env.user.has_group('KSW_documents.group_document_officer')
        for owner in self:
            owner.x_document_pending_my_action = mine and owner.x_document_issue

    def _search_document_pending(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        if not (self.env.su or self.env.user.has_group('KSW_documents.group_document_officer')):
            return [('id', '=', False)] if wanted else []
        return self._search_document_issue('=', wanted)

    def _ksw_doc_owner_field(self):
        raise NotImplementedError

    def _ksw_required_types(self):
        """{owner id: required ksw.document.type recordset} for these owners."""
        raise NotImplementedError

    def _ksw_document_gaps(self):
        """{owner id: [type names missing or expired]}"""
        field = self._ksw_doc_owner_field()
        required = self._ksw_required_types()
        docs = self.env['ksw.document'].sudo().search([(field, 'in', self.ids), ('state', '=', 'confirmed')])
        current = {}
        for doc in docs:
            current[(doc[field].id, doc.type_id.id)] = doc
        gaps = {}
        for owner in self:
            names = list(owner._ksw_document_preconditions())
            for doc_type in required.get(owner.id, self.env['ksw.document.type']):
                doc = current.get((owner.id, doc_type.id))
                if not doc:
                    names.append(doc_type.name)
                elif doc.expiry_status == 'expired':
                    names.append(_('%s (expired)', doc_type.name))
                elif doc.expiry_status == 'unknown':
                    names.append(_('%s (expiry not entered)', doc_type.name))
            gaps[owner.id] = names
        return gaps

    def _ksw_handover_gaps(self):
        """{owner id: [(type, 'missing' | 'expired')]}: what blocks a vehicle
        handover. The register's requirements, narrowed to the types HR
        checks at a handover."""
        field = self._ksw_doc_owner_field()
        required = self._ksw_required_types()
        docs = self.env['ksw.document'].sudo().search([(field, 'in', self.ids), ('state', '=', 'confirmed')])
        current = {(doc[field].id, doc.type_id.id): doc for doc in docs}
        gaps = {}
        for owner in self:
            found = []
            for doc_type in required.get(owner.id, self.env['ksw.document.type']).filtered('handover_check'):
                doc = current.get((owner.id, doc_type.id))
                if not doc:
                    found.append((doc_type, 'missing'))
                elif doc.expiry_status == 'expired':
                    found.append((doc_type, 'expired'))
            gaps[owner.id] = found
        return gaps

    def _compute_documents_missing(self):
        gaps = self._ksw_document_gaps()
        for owner in self:
            owner.x_documents_missing = ', '.join(gaps.get(owner.id, [])) or False
            owner.x_document_issue = bool(gaps.get(owner.id))

    def _search_document_issue(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        candidates = self._ksw_requirement_candidates()
        gaps = candidates._ksw_document_gaps()
        ids = [oid for oid, names in gaps.items() if names]
        return [('id', 'in' if wanted else 'not in', ids)]

    def _ksw_requirement_candidates(self):
        raise NotImplementedError

    def _ksw_document_preconditions(self):
        """What must be known before the requirements can be: nothing by default."""
        return []


class KswFleetVehicle(models.Model):
    _name = 'ksw.fleet.vehicle'
    _inherit = ['ksw.fleet.vehicle', 'ksw.document.owner.mixin']

    x_document_ids = fields.One2many('ksw.document', 'vehicle_id', string='Documents',
                                     domain=[('state', '!=', 'replaced')])

    def _ksw_doc_owner_field(self):
        return 'vehicle_id'

    def _ksw_requirement_candidates(self):
        own = self.env['res.company'].sudo().search([]).partner_id
        return self.sudo().search([('client_id', 'in', own.ids), ('state', '=', 'confirmed')])

    def _ksw_required_types(self):
        types = self.env['ksw.document.type'].sudo().search(
            [('applies_to', '=', 'vehicle'), ('required_for', 'in', ('own_fleet', 'public_transport',
                                                                     'water_tankers'))])
        own = set(self.env['res.company'].sudo().search([]).partner_id.ids)
        result = {}
        for v in self.sudo():
            # A tedder runs under its head's papers (the tank licence is issued
            # on the head): it needs a head, not documents of its own.
            if v.client_id.id not in own or v.state != 'confirmed' or v.vehicle_type == 'tank':
                result[v.id] = types.browse()
                continue
            # The operation card is for public transport only (Naql).
            result[v.id] = types.filtered(lambda t: t.required_for == 'own_fleet' or (
                t.required_for == 'public_transport' and v.x_registration_type == 'public') or (
                # The tank licence is issued on the vehicle that carries the tank:
                # an Isuzu tanker, or a head while it pulls a tedder.
                t.required_for == 'water_tankers' and (v.vehicle_type == 'isuzu' or (
                    v.vehicle_type == 'trailer' and 'x_tedder_ids' in v._fields and v.x_tedder_ids))))
        return result

    # A tedder needs no documents of its own: coupled, it runs under its
    # head's (the tank licence is issued on the head); uncoupled, it is
    # parked — a head-less tedder cannot move, so nothing of it is missing.
    # Which ones wait for a head is the Tedders app's «Not Coupled» filter.

    def action_upload_documents(self):
        self.ensure_one()
        return self.env['ksw.document.upload.wizard']._open_for(vehicle=self)


class HrEmployee(models.Model):
    _name = 'hr.employee'
    _inherit = ['hr.employee', 'ksw.document.owner.mixin']

    x_document_ids = fields.One2many('ksw.document', 'employee_id', string='Documents',
                                     domain=[('state', '!=', 'replaced')], groups='hr.group_hr_user')
    # HR only: a regular employee reads his own record through the public
    # model, which has none of these (gotcha #32).
    x_documents_missing = fields.Char(groups='hr.group_hr_user,base.group_system')
    x_document_issue = fields.Boolean(groups='hr.group_hr_user,base.group_system')
    x_document_pending_my_action = fields.Boolean(groups='hr.group_hr_user,base.group_system')

    def _ksw_doc_owner_field(self):
        return 'employee_id'

    def _ksw_custodian_ids(self):
        return set(self.env['ksw.fleet.vehicle'].sudo().search(
            [('x_custodian_id', '!=', False)]).x_custodian_id.ids)

    def _ksw_requirement_candidates(self):
        return self.sudo().search([])

    def _ksw_required_types(self):
        types = self.env['ksw.document.type'].sudo().search(
            [('applies_to', '=', 'employee'), ('required_for', '!=', 'none')])
        custodians = self._ksw_custodian_ids()
        result = {}
        for employee in self.sudo():
            # Only a nationality we know decides iqama/passport: an employee
            # with none recorded is asked for his nationality, not his iqama.
            non_saudi = bool(employee.country_id) and employee.country_id.code != 'SA'
            result[employee.id] = types.filtered(lambda t: (
                t.required_for == 'all'
                or (t.required_for == 'non_saudi' and non_saudi)
                or (t.required_for == 'custodians' and (
                    employee.id in custodians or self.env.context.get('ksw_as_custodian')))))
        return result

    def _ksw_document_preconditions(self):
        self.ensure_one()
        if not self.sudo().country_id and self.env['ksw.document.type'].sudo().search_count(
                [('applies_to', '=', 'employee'), ('required_for', '=', 'non_saudi')]):
            return [_('Nationality not recorded')]
        return []

    def action_upload_documents(self):
        self.ensure_one()
        return self.env['ksw.document.upload.wizard']._open_for(employee=self)
