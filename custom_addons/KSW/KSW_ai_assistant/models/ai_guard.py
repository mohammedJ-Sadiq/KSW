"""What the AI assistant may read for the asking user: the security policy.

The rule is **default deny**: a field is readable through the assistant
only if the user's own screens of that model *display* it.

Record rules and ``fields_get`` are not enough. KSW hides much of its
sensitive data at VIEW level (gross salary on a loan, the vacation
settlement on a leave, payroll impact panels): those fields carry no
model-level ``groups=`` on purpose (pitfall #31), are often
``compute_sudo``, and are only stripped from the screens of users outside
the right groups. Reading them by name would hand any user who can open the
record the very figures the screen withholds.

So for every model the allowed set is taken from the user's own
list / form / kanban / search views, after Odoo removed what his groups may
not see, minus anything that is always invisible (helper fields Odoo or a
view adds only to drive ``invisible=`` expressions). Every use is checked:
the columns read, every hop of every domain path (so a salary cannot be
narrowed down by filtering or sorting on it), the sort order, the grouping
and the aggregated measures.

On top of that, a hard block that no view can lift: credentials, bank
accounts and personal identifiers are never sent to the AI provider.
"""
import logging
import re

from lxml import etree

from odoo import api, models

_logger = logging.getLogger(__name__)

# Never offered, whatever the menus or the views say.
TECHNICAL_PREFIX = ('ir.', 'base.', 'bus.', 'res.config', 'base_import.',
                    'mail.', 'auth_', 'iap.', 'fetchmail.', 'ksw.ai.',
                    'digest.', 'web_tour.', 'spreadsheet', 'rating.')
BLOCKED_MODELS = {
    # credentials and access control
    'res.users', 'res.users.apikeys', 'res.users.settings', 'res.users.log',
    'res.groups', 'res.lang',
    # bank accounts and personal records of other people
    'res.partner.bank', 'resource.resource', 'hr.resume.line',
    'hr.employee.skill', 'hr.leave.report.calendar',
}
# Field names never read or sent, on any model: credentials, bank details,
# personal identifiers and private contact data.
SENSITIVE_RE = re.compile(
    r'pass(word)?\b|passwd|secret|token|api_?key|signature|totp|\bpin\b|'
    r'iban|acc_number|bank_acc|bank_account|swift|bic\b|'
    r'identification|ssnid|ssn\b|iqama|passport|permit_no|visa_no|'
    r'private_|birthday|place_of_birth|country_of_birth|spouse|'
    r'emergency_|km_home_work|barcode|pin_code', re.I)
# Pay data: on top of the screens, the assistant shows other people's pay
# only to the roles whose job it is. A manager may see his team's
# payslips on screen one by one; the assistant must not turn that into
# "list my whole team's salaries, highest first".
PAYROLL_MODELS = {
    'hr.payslip', 'hr.payslip.line', 'hr.payslip.input',
    'hr.payslip.worked_days', 'hr.payslip.run', 'hr.version',
    'ksw.payslip.revision.request', 'ksw.payslip.pending.change',
}
PAY_RE = re.compile(
    r'wage|salary|gross|(^|_)net($|_)|net_payable|vacation_calc|vacation_bal|'
    r'eos_\w*(amount|wage)|hourly|daily_rate|allowance|(^|_)basic($|_)|'
    r'(^|_)hra($|_)|gosi|prior_net|deserved', re.I)
PAY_PRIVILEGED_GROUPS = (
    'om_hr_payroll.group_hr_payroll_user',      # Payroll Officer and above
    'hr.group_hr_manager',
    'KSW_deduction.group_deduction_officer',
    'KSW_deduction.group_deduction_manager',
    'KSW_deduction.group_loan_hr',
    'KSW_deduction.group_loan_acc',
    'KSW_deduction.group_loan_gm',
)
_ALWAYS = {'id', 'display_name'}
_HIDDEN = ('1', 'True', 'true')
_SUBVIEW_TAGS = ('list', 'tree', 'form', 'kanban')


class ToolInputError(Exception):
    """Bad or forbidden tool input: returned to the model as an error."""


class KswAiGuard(models.AbstractModel):
    _name = 'ksw.ai.guard'
    _description = 'AI Assistant Access Guard'

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------
    @api.model
    def _model_blocked(self, model):
        return (not model or model.startswith(TECHNICAL_PREFIX)
                or model in BLOCKED_MODELS)

    # ------------------------------------------------------------------
    # Fields the user's screens display
    # ------------------------------------------------------------------
    @api.model
    def _visible(self, model):
        """``{model: set(fields)}`` displayed by the user's own views of
        ``model`` (the model itself, plus the sub-tables those views
        embed). Cached per user and model for the request."""
        cache = self.env.cr.cache.setdefault('ksw_ai_visible', {})
        key = (self.env.uid, model)
        if key in cache:
            return cache[key]
        result = {}
        Model = self.env[model]
        for vtype in ('list', 'form', 'kanban', 'search'):
            try:
                views = Model.get_views([(False, vtype)])
            except Exception:  # noqa: BLE001 - no such view: nothing shown
                continue
            arch = views['views'].get(vtype, {}).get('arch')
            if not arch:
                continue
            try:
                root = etree.fromstring(arch)
            except etree.XMLSyntaxError:
                continue
            self._walk(root, model, False, result)
        cache[key] = result
        return result

    @api.model
    def _walk(self, node, model, hidden, result):
        hidden = hidden or node.get('invisible') in _HIDDEN \
            or node.get('column_invisible') in _HIDDEN
        if node.tag == 'field' and node.get('name'):
            name = node.get('name')
            if not hidden:
                result.setdefault(model, set()).add(name)
            field = self.env[model]._fields.get(name)
            comodel = field.comodel_name if field is not None \
                and field.relational else None
            for child in node:
                if comodel and child.tag in _SUBVIEW_TAGS:
                    self._walk(child, comodel, hidden, result)
            return
        for child in node:
            self._walk(child, model, hidden, result)

    @api.model
    def _allowed(self, model, via=None):
        """Fields of ``model`` the assistant may read for this user.

        ``via``: the parent model whose views embed ``model`` as a
        sub-table; its sub-view columns count too.
        """
        allowed = set(self._visible(model).get(model, set()))
        if via:
            allowed |= self._visible(via).get(model, set())
        fields = self.env[model]._fields
        return {f for f in allowed
                if f in fields and not SENSITIVE_RE.search(f)} | _ALWAYS

    @api.model
    def _check_field(self, model, fname, via=None):
        if SENSITIVE_RE.search(fname or ''):
            raise ToolInputError(
                'Field %s holds personal or credential data and is never '
                'available to the assistant.' % fname)
        if fname not in self._allowed(model, via=via):
            raise ToolInputError(
                'Field %s of %s is not shown on this user\'s screens, so it '
                'is not available. Use get_fields.' % (fname, model))

    # ------------------------------------------------------------------
    # Domains, order, grouping
    # ------------------------------------------------------------------
    @api.model
    def _hop_model(self, model):
        """The model whose screens decide a path hop into ``model``: a
        non-HR user reads employees through ``hr.employee.public``."""
        Model = self.env[model]
        if Model.has_access('read'):
            return model
        if model == 'hr.employee' and \
                self.env['hr.employee.public'].has_access('read'):
            return 'hr.employee.public'
        return None

    @api.model
    def _check_path(self, model, path):
        """Every hop of a dotted path must be displayed to the user."""
        current = model
        parts = path.split('.')
        for i, part in enumerate(parts):
            if self._model_blocked(current):
                raise ToolInputError('%s is not available.' % current)
            screen_model = self._hop_model(current)
            if not screen_model:
                raise ToolInputError(
                    'The user has no access to %s.' % current)
            self._check_field(screen_model, part)
            field = self.env[current]._fields.get(part)
            if field is None:
                raise ToolInputError('Unknown field %s on %s.'
                                     % (part, current))
            if i < len(parts) - 1:
                if not field.relational:
                    raise ToolInputError('%s is not a relation.' % part)
                current = field.comodel_name
        return current

    @api.model
    def _check_domain(self, model, raw):
        """Walk a raw domain (lists, '&' '|' '!', and 'any' sub-domains)."""
        for item in raw:
            if isinstance(item, str):
                if item not in ('&', '|', '!'):
                    raise ToolInputError('Bad domain operator %r.' % item)
                continue
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise ToolInputError('Bad domain term %r.' % (item,))
            path, op, value = item
            if not isinstance(path, str):
                # (1, '=', 1) style constants carry no field
                continue
            target = self._check_path(model, path)
            if op in ('any', 'not any') and isinstance(value, list):
                self._check_domain(target, value)

    @api.model
    def _check_order(self, model, order):
        if not order:
            return
        for part in order.split(','):
            tokens = part.strip().split()
            if not tokens:
                continue
            if len(tokens) > 2 or (len(tokens) == 2 and tokens[1].lower()
                                   not in ('asc', 'desc')):
                raise ToolInputError('Bad order %r.' % order)
            self._check_path(model, tokens[0])

    # ------------------------------------------------------------------
    # Pay confidentiality
    # ------------------------------------------------------------------
    @api.model
    def _pay_privileged(self):
        user = self.env.user
        for xmlid in PAY_PRIVILEGED_GROUPS:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group and group in user.all_group_ids:
                return True
        return False

    @api.model
    def _is_pay(self, model, names):
        """True when ``model`` is a payroll document or any of ``names``
        (field names or dotted paths) is a pay figure."""
        return model in PAYROLL_MODELS or any(
            PAY_RE.search(part) for name in names if name
            for part in name.split('.'))

    @api.model
    def _own_domain(self, model):
        fields = self.env[model]._fields
        if model in ('hr.employee', 'hr.employee.public'):
            return [('user_id', '=', self.env.uid)]
        if 'employee_id' in fields:
            return [('employee_id.user_id', '=', self.env.uid)]
        if 'slip_id' in fields:
            return [('slip_id.employee_id.user_id', '=', self.env.uid)]
        return None

    @api.model
    def _pay_scope(self, model, names):
        """Extra domain limiting a query that touches pay to the user's own
        records; ``[]`` when no limit applies. Raises when the model has no
        owner to limit to (a payslip batch, say)."""
        if not self._is_pay(model, names) or self._pay_privileged():
            return []
        own = self._own_domain(model)
        if own is None:
            raise ToolInputError(
                'Pay figures of other employees are only available to '
                'payroll officers and HR managers.')
        return own

    @api.model
    def _record_is_own(self, record):
        record = record.sudo()
        if record._name in ('hr.employee', 'hr.employee.public'):
            return record.user_id.id == self.env.uid
        if 'employee_id' in record._fields:
            return record.employee_id.user_id.id == self.env.uid
        if 'slip_id' in record._fields:
            return record.slip_id.employee_id.user_id.id == self.env.uid
        return False

