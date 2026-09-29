"""Audit every change to users, employees, contracts and salary accounts.

Generic on purpose: the watched fields are whatever is written, minus noise,
so a field added by any KSW module (x_is_attendance_sheet, main_calendar_id,
struct_id, allowances...) is audited without this module naming it.
"""
from markupsafe import Markup

from odoo import _, api, fields, models

_NOISE = frozenset({
    'write_date', 'write_uid', 'create_date', 'create_uid', '__last_update',
    'display_name', 'login_date', 'share', 'signature', 'tz_offset',
    'totp_last_counter', 'rules_count', 'groups_count', 'accesses_count',
})
_NOISE_PREFIXES = ('message_', 'activity_', 'website_message', 'image_',
                   'avatar_', 'rating_')
_SECRET_WORDS = ('password', 'secret', 'token', 'totp', 'api_key', 'pin')
_MAX_O2M = 20


def _auditable(model, fname):
    field = model._fields.get(fname)
    if not field or fname in _NOISE or fname.startswith(_NOISE_PREFIXES):
        return False
    if field.type == 'binary':
        return False
    if field.related or getattr(field, 'inherited', False):
        # Written through to its source record, which logs it there.
        return False
    return field.store or field.type == 'many2many'


_CHATTER_SAFE_GROUPS = {'base.group_user', 'hr.group_hr_user'}


def _hide_in_chatter(field):
    """Employee chatter is read by HR users; hide only what is stricter."""
    groups = set(filter(None, (field.groups or '').split(',')))
    return bool(groups - _CHATTER_SAFE_GROUPS)


def _is_secret(fname):
    return any(word in fname for word in _SECRET_WORDS)


def _repr(record, fname):
    field = record._fields[fname]
    value = record[fname]
    if _is_secret(fname):
        return _('(set)') if value else ''
    if field.type == 'many2one':
        return value.display_name or ''
    if field.type in ('many2many', 'one2many'):
        names = sorted(value.mapped('display_name'))
        if field.type == 'one2many' and len(names) > _MAX_O2M:
            return _('%s lines', len(names))
        return ', '.join(names)
    if field.type == 'selection':
        return dict(field._description_selection(record.env)).get(value, value or '')
    if field.type == 'boolean':
        return _('Yes') if value else _('No')
    if value is False or value is None:
        return ''
    return str(value)


class KswAuditMixin(models.AbstractModel):
    _name = 'ksw.audit.mixin'
    _description = 'Audit trail hooks'

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------
    def _ksw_audit_enabled(self):
        return not self.env.context.get('install_mode') \
            and not self.env.context.get('ksw_audit_skip')

    def _ksw_audit_records(self):
        """The subset of self that is audited (bank accounts: employees' only)."""
        return self

    def _ksw_audit_employees(self):
        """Employees whose chatter carries this record's changes."""
        return self.env['hr.employee']

    def _ksw_audit_chatter_target(self):
        """Where to post when there is no employee (e.g. a user's contact)."""
        return self.env['res.partner']

    # ------------------------------------------------------------------
    # Engine
    # ------------------------------------------------------------------
    def _ksw_audit_snapshot(self, fnames):
        return {rec.id: {f: _repr(rec, f) for f in fnames}
                for rec in self.sudo()}

    def _ksw_audit_entry(self, rec, operation, fname='', old='', new=''):
        field = rec._fields.get(fname) if fname else None
        employee = rec._ksw_audit_employees()[:1]
        return {
            'user_id': self.env.uid,
            'operation': operation,
            'model': rec._name,
            'model_label': rec._description,
            'res_id': rec.id,
            'record_name': rec.sudo().display_name,
            'employee_id': employee.id or False,
            'field_name': fname,
            'field_label': field.string if field else '',
            'old_value': old,
            'new_value': new,
        }

    def _ksw_audit_post(self, rec, lines, title):
        """Post the change to the employee's (or contact's) chatter."""
        if not lines:
            return
        body = Markup('<strong>%s</strong><ul>') % title
        for field, old, new in lines:
            if _hide_in_chatter(field) or _is_secret(field.name):
                body += Markup('<li><b>%s</b>: %s</li>') % (
                    field.string, _('changed — see the Audit Log'))
            else:
                body += Markup('<li><b>%s</b>: %s &#8594; %s</li>') % (
                    field.string, old or _('(empty)'), new or _('(empty)'))
        body += Markup('</ul>')
        targets = rec.sudo()._ksw_audit_employees()
        if not targets:
            targets = rec.sudo()._ksw_audit_chatter_target()
        for target in targets:
            target.sudo().message_post(
                body=body, subtype_xmlid='mail.mt_note',
                author_id=self.env.user.partner_id.id)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if records._ksw_audit_enabled():
            audited = records._ksw_audit_records()
            entries = [self._ksw_audit_entry(rec, 'create') for rec in audited]
            self.env['ksw.audit.log']._log(entries)
        return records

    def write(self, vals):
        if not self._ksw_audit_enabled():
            return super().write(vals)
        audited = self._ksw_audit_records()
        fnames = [f for f in vals if _auditable(self, f)]
        before = audited._ksw_audit_snapshot(fnames) if (audited and fnames) else {}
        res = super().write(vals)
        if not before:
            return res
        after = audited._ksw_audit_snapshot(fnames)
        entries = []
        for rec in audited.sudo():
            lines = []
            for fname in fnames:
                old, new = before[rec.id][fname], after[rec.id][fname]
                if old == new:
                    continue
                entries.append(self._ksw_audit_entry(rec, 'write', fname, old, new))
                lines.append((rec._fields[fname], old, new))
            self._ksw_audit_post(rec, lines, _('%(what)s updated by %(user)s',
                                               what=rec._description,
                                               user=self.env.user.name))
        self.env['ksw.audit.log']._log(entries)
        return res

    def unlink(self):
        if self._ksw_audit_enabled():
            entries = [self._ksw_audit_entry(rec, 'unlink')
                       for rec in self._ksw_audit_records()]
            self.env['ksw.audit.log']._log(entries)
        return super().unlink()


class HrEmployee(models.Model):
    _name = 'hr.employee'
    _inherit = ['hr.employee', 'ksw.audit.mixin']

    def _ksw_audit_employees(self):
        return self


class HrVersion(models.Model):
    _name = 'hr.version'
    _inherit = ['hr.version', 'ksw.audit.mixin']

    def _ksw_audit_employees(self):
        return self.sudo().employee_id


class ResUsers(models.Model):
    _name = 'res.users'
    _inherit = ['res.users', 'ksw.audit.mixin']

    def _ksw_audit_employees(self):
        return self.sudo().employee_ids

    def _ksw_audit_chatter_target(self):
        return self.sudo().partner_id

    def write(self, vals):
        res = super().write(vals)
        if ({'password', 'new_password'} & set(vals)) and self._ksw_audit_enabled():
            field = self._fields['password']
            self.env['ksw.audit.log']._log([
                self._ksw_audit_entry(rec, 'write', 'password', '', _('(changed)'))
                for rec in self])
            for rec in self:
                self._ksw_audit_post(rec, [(field, '', _('(changed)'))], _(
                    'Password changed by %s', self.env.user.name))
        return res


class ResPartnerBank(models.Model):
    """Salary bank accounts: only those attached to an employee."""
    _name = 'res.partner.bank'
    _inherit = ['res.partner.bank', 'ksw.audit.mixin']

    def _ksw_audit_employees(self):
        return self.env['hr.employee'].sudo().with_context(
            active_test=False).search([('bank_account_ids', 'in', self.ids)])

    def _ksw_audit_records(self):
        if not self:
            return self
        linked = self.env['hr.employee'].sudo().with_context(
            active_test=False).search([('bank_account_ids', 'in', self.ids)])
        linked_ids = set(linked.bank_account_ids.ids)
        return self.filtered(lambda b: b.id in linked_ids)
