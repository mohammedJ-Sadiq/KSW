# -*- coding: utf-8 -*-
"""AI assistant: the tools only read and only as the asking user, the
question loop keeps a valid append-only history, and the UI renders.

The Claude API is replaced by scripted responses: these tests check our
side of the contract, not the model.
"""
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.KSW_ai_assistant.models import ai_conversation
from odoo.addons.KSW_ai_assistant.models.ai_tools import TOOLS, ToolInputError
from odoo.addons.KSW_ai_assistant.models.claude_client import ClaudeError


def _tool_use(tid, name, args):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': args}


def _reply(content, stop):
    return {'content': content, 'stop_reason': stop,
            'usage': {'input_tokens': 100, 'output_tokens': 20,
                      'cache_read_input_tokens': 50}}


def _partner_menu(env):
    """A Contacts menu of our own: the catalog only offers models the user
    reaches through a menu, and a bare database has no Contacts app."""
    action = env['ir.actions.act_window'].create({
        'name': 'AI Test Contacts', 'res_model': 'res.partner',
        'view_mode': 'list,form'})
    env['ir.ui.menu'].create({
        'name': 'AI Test Contacts', 'action': 'ir.actions.act_window,%d'
        % action.id, 'group_ids': [(4, env.ref('base.group_user').id)]})
    return action


@tagged('post_install', '-at_install')
class TestAiAssistant(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('ksw_ai.api_key', 'test-key')
        ICP.set_param('ksw_ai.provider', 'anthropic')
        cls.user = cls.env['res.users'].create({
            'name': 'AI Plain User', 'login': 'ai_plain_user',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.other = cls.env['res.users'].create({
            'name': 'AI Other User', 'login': 'ai_other_user',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.Conv = cls.env['ksw.ai.conversation']
        _partner_menu(cls.env)
        cls.env.registry.clear_cache()

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def test_no_tool_writes(self):
        self.assertEqual(
            sorted(t['name'] for t in TOOLS),
            ['find_models', 'get_fields', 'read_record', 'search_records',
             'summarize_records'])

    def test_read_record_lists_values_and_lines(self):
        company = self.env['res.partner'].create({
            'name': 'AI Read Co', 'is_company': True,
            'child_ids': [(0, 0, {'name': 'AI Read Contact'})]})
        result, _block = self.env['ksw.ai.tools']._run_tool(
            'read_record', {'model': 'res.partner',
                            'record_id': company.id})
        self.assertEqual(result['name'], 'AI Read Co')
        self.assertTrue(any('AI Read Co' in v
                            for v in result['fields'].values()))
        self.assertIn('child_ids', result['subtables'])
        self.assertIn(['AI Read Contact'],
                      [r[:1] for r in result['subtables']['child_ids']['rows']])

    def test_read_record_missing_is_retryable(self):
        with self.assertRaises(ToolInputError):
            self.env['ksw.ai.tools']._run_tool(
                'read_record', {'model': 'res.partner', 'record_id': 0})

    def test_page_line_names_the_open_record(self):
        partner = self.env['res.partner'].create({'name': 'AI Page Co'})
        line = self.Conv._ksw_page_line(
            {'model': 'res.partner', 'res_id': partner.id})
        self.assertIn('CURRENT RECORD', line)
        self.assertIn('AI Page Co', line)
        self.assertIn('id %s' % partner.id, line)
        # A model outside the user's menus, or junk, says nothing.
        self.assertEqual(self.Conv._ksw_page_line(
            {'model': 'ir.config_parameter', 'res_id': 1}), '')
        self.assertEqual(self.Conv._ksw_page_line('junk'), '')

    def test_page_line_hides_a_record_the_user_cannot_read(self):
        group = self.env.ref('base.group_system')
        rule_model = self.env['ir.model']._get('res.partner')
        self.env['ir.rule'].create({
            'name': 'AI test: hide one partner',
            'model_id': rule_model.id,
            'domain_force': "[('name', '!=', 'AI Secret Co')]",
            'groups': [(4, self.env.ref('base.group_user').id)],
        })
        secret = self.env['res.partner'].create({'name': 'AI Secret Co'})
        line = self.Conv.with_user(self.user)._ksw_page_line(
            {'model': 'res.partner', 'res_id': secret.id})
        self.assertNotIn('AI Secret Co', line)

    def test_arabic_question_asks_for_an_arabic_reply(self):
        seen = []

        def fake(key, model, system, messages, tools, effort='medium'):
            seen.append(messages[-1]['content'])
            return _reply([{'type': 'text', 'text': 'تم'}], 'end_turn')

        with patch.object(ai_conversation, 'create_message', fake):
            self.Conv.ksw_ai_ask('كم عدد القروض؟')
        self.assertIn('Reply in Arabic', seen[0])

    def test_catalog_follows_the_users_menus(self):
        admin = self.env['ksw.ai.tools']._catalog()
        plain = self.env['ksw.ai.tools'].with_user(self.user)._catalog()
        self.assertTrue(set(plain) <= set(admin))
        self.assertLess(len(plain), len(admin))
        self.assertNotIn('ir.config_parameter', plain)

    def test_model_outside_catalog_is_refused(self):
        tools = self.env['ksw.ai.tools'].with_user(self.user)
        with self.assertRaises(ToolInputError):
            tools._run_tool('search_records', {
                'model': 'ir.config_parameter', 'domain': '[]',
                'fields': ['key', 'value'], 'order': '', 'limit': 5,
                'title': 'x'})

    def test_bad_field_and_bad_json_are_retryable_errors(self):
        tools = self.env['ksw.ai.tools']
        for domain in ('[["no_such_field", "=", 1]]', 'not json'):
            with self.assertRaises(ToolInputError):
                tools._run_tool('search_records', {
                    'model': 'res.partner', 'domain': domain,
                    'fields': ['name'], 'order': '', 'limit': 5,
                    'title': 'x'})

    def test_search_runs_as_the_user(self):
        """Record rules apply: a user's search never returns what he
        could not open himself."""
        tools = self.env['ksw.ai.tools'].with_user(self.user)
        mine = self.env['res.partner'].with_user(self.user).search_count([])
        result, block = tools._run_tool('search_records', {
            'model': 'res.partner', 'domain': '[]', 'fields': ['name'],
            'order': '', 'limit': 5, 'title': 'Contacts'})
        self.assertEqual(result['total_matching'], mine)
        self.assertEqual(block['filter_text'], 'All records')

    def test_summary_counts_and_filter_text(self):
        self.env['res.partner'].create([
            {'name': 'AI Sum A', 'is_company': True},
            {'name': 'AI Sum B', 'is_company': True},
        ])
        result, block = self.env['ksw.ai.tools']._run_tool(
            'summarize_records', {
                'model': 'res.partner',
                'domain': '[["name", "ilike", "AI Sum"]]',
                'group_by': ['is_company'], 'measures': [],
                'title': 'Companies'})
        self.assertEqual(block['total'], 2)
        self.assertIn('AI Sum', block['filter_text'])

    def test_filter_text_shows_names_not_ids(self):
        parent = self.env['res.partner'].create({'name': 'AI Parent Co'})
        result, block = self.env['ksw.ai.tools']._run_tool(
            'search_records', {
                'model': 'res.partner',
                'domain': json.dumps([['parent_id', '=', parent.id]]),
                'fields': ['name', 'parent_id.name'], 'order': '',
                'limit': 5, 'title': 'x'})
        self.assertIn('AI Parent Co', block['filter_text'])
        self.assertNotIn(str(parent.id), block['filter_text'])

    # ------------------------------------------------------------------
    # The question loop
    # ------------------------------------------------------------------
    def test_ask_runs_tools_then_answers(self):
        script = [
            _reply([_tool_use('t1', 'search_records', {
                'model': 'res.partner', 'domain': '[]', 'fields': ['name'],
                'order': '', 'limit': 3, 'title': 'Contacts'}),
                _tool_use('t2', 'find_models', {'keywords': ['contact']})],
                'tool_use'),
            _reply([{'type': 'text', 'text': 'Found **3** contacts.'}],
                   'end_turn'),
        ]
        sent = []

        def fake(key, model, system, messages, tools, effort='medium'):
            sent.append(json.loads(json.dumps(messages)))
            return script.pop(0)

        with patch.object(ai_conversation, 'create_message', fake):
            res = self.Conv.ksw_ai_ask('show contacts')
        self.assertEqual(res['answer'], 'Found **3** contacts.')
        self.assertEqual(len(res['blocks']), 1)
        # Both tool results go back in ONE user message, matched by id.
        second_call = sent[1]
        results = second_call[-1]['content']
        self.assertEqual([r['tool_use_id'] for r in results], ['t1', 't2'])
        conv = self.Conv.browse(res['conversation_id'])
        roles = [m['role'] for m in conv.api_messages]
        self.assertEqual(roles, ['user', 'assistant', 'user', 'assistant'])
        turn = conv.turn_ids
        self.assertEqual(turn.input_tokens, 200)
        self.assertEqual(len(turn.tool_calls), 2)

    def test_api_failure_leaves_history_unchanged(self):
        def boom(*args, **kwargs):
            raise ClaudeError('rate_limit')

        with patch.object(ai_conversation, 'create_message', boom):
            res = self.Conv.ksw_ai_ask('anything')
        self.assertTrue(res['error'])
        conv = self.Conv.browse(res['conversation_id'])
        self.assertFalse(conv.api_messages)
        self.assertEqual(conv.turn_ids.error, 'rate_limit')

    def test_cannot_continue_someone_elses_conversation(self):
        def answer(*args, **kwargs):
            return _reply([{'type': 'text', 'text': 'ok'}], 'end_turn')

        with patch.object(ai_conversation, 'create_message', answer):
            theirs = self.Conv.with_user(self.other).ksw_ai_ask('hello')
            mine = self.Conv.with_user(self.user).ksw_ai_ask(
                'hello', theirs['conversation_id'])
        self.assertNotEqual(mine['conversation_id'],
                            theirs['conversation_id'])
        self.assertFalse(self.Conv.with_user(self.user).search(
            [('id', '=', theirs['conversation_id'])]))

    def test_openai_compatible_path(self):
        """Groq / Ollama: tool calls come back as role=tool messages, and
        only the question and the final answer are kept for next time."""
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_ai.provider', 'groq')
        script = [
            {'choices': [{'message': {'content': '', 'tool_calls': [{
                'id': 'c1', 'type': 'function', 'function': {
                    'name': 'search_records',
                    'arguments': json.dumps({
                        'model': 'res.partner', 'domain': '[]',
                        'fields': ['name'], 'order': '', 'limit': 3,
                        'title': 'Contacts'})}}]}}],
             'usage': {'prompt_tokens': 900, 'completion_tokens': 40}},
            {'choices': [{'message': {'content': 'Three contacts.'}}],
             'usage': {'prompt_tokens': 1200, 'completion_tokens': 10}},
        ]
        sent = []

        def fake(base_url, key, model, messages, tools, max_tokens=4096,
                 effort='high'):
            sent.append(json.loads(json.dumps(messages)))
            self.assertEqual(base_url, 'https://api.groq.com/openai/v1')
            self.assertEqual(tools[0]['type'], 'function')
            return script.pop(0)

        with patch.object(ai_conversation, 'chat_completion', fake):
            res = self.Conv.ksw_ai_ask('show contacts')
        self.assertEqual(res['answer'], 'Three contacts.')
        self.assertEqual(len(res['blocks']), 1)
        tool_msg = sent[1][-1]
        self.assertEqual((tool_msg['role'], tool_msg['tool_call_id']),
                         ('tool', 'c1'))
        conv = self.Conv.browse(res['conversation_id'])
        self.assertEqual([m['role'] for m in conv.api_messages],
                         ['user', 'assistant'])
        self.assertEqual(conv.turn_ids.input_tokens, 2100)

    def test_no_key_means_a_clear_message(self):
        self.env['ir.config_parameter'].sudo().set_param('ksw_ai.api_key',
                                                         False)
        with patch.dict('os.environ', {}, clear=False) as env:
            env.pop('ANTHROPIC_API_KEY', None)
            with self.assertRaises(UserError):
                self.Conv.ksw_ai_ask('anything')


@tagged('post_install', '-at_install')
class TestAiAssistantUI(HttpCase):

    def test_form_page_reaches_the_chat(self):
        """Opened from a contact form, the chat shows "About: <record>"."""
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_ai.api_key', 'test-key')
        partner = self.env['res.partner'].create({'name': 'AI Chip Co'})
        action = _partner_menu(self.env)
        self.browser_js(
            '/odoo/action-%d/%d' % (action.id, partner.id),
            """
            const wait = (sel) => new Promise((ok, ko) => {
                let n = 0;
                const t = setInterval(() => {
                    const el = document.querySelector(sel);
                    if (el) { clearInterval(t); ok(el); }
                    if (++n > 150) { clearInterval(t); ko(sel + ' not found'); }
                }, 100);
            });
            wait('.o_form_view').then(() => wait('.ksw_ai_systray'))
              .then((btn) => { btn.click(); return wait('.ksw_ai_page'); })
              .then((chip) => {
                  if (!chip.textContent.includes('AI Chip Co')) {
                      throw new Error('chip shows ' + chip.textContent);
                  }
                  console.log('test successful');
              })
              .catch((e) => console.error(e));
            """,
            login='admin',
        )

    def test_home_shows_ask_box_and_systray(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_ai.api_key', 'test-key')
        self.browser_js(
            '/odoo',
            """
            const wait = (sel) => new Promise((ok, ko) => {
                let n = 0;
                const t = setInterval(() => {
                    if (document.querySelector(sel)) { clearInterval(t); ok(); }
                    if (++n > 100) { clearInterval(t); ko(sel + ' not found'); }
                }, 100);
            });
            Promise.all([
                wait('.ksw_ai_home .ksw_ai_input textarea'),
                wait('.ksw_ai_systray'),
            ]).then(() => console.log('test successful'))
              .catch((e) => console.error(e));
            """,
            login='admin',
        )


@tagged('post_install', '-at_install')
class TestAiSecurity(TransactionCase):
    """The guard: only what the user's screens display, never credentials
    or personal identifiers, pay of others only for payroll roles."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Guard = cls.env['ksw.ai.guard']
        cls.Tools = cls.env['ksw.ai.tools']
        cls.user = cls.env['res.users'].create({
            'name': 'AI Sec User', 'login': 'ai_sec_user',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])]})
        cls.partner_action = _partner_menu(cls.env)

    def _walk(self, arch, model='res.partner'):
        from lxml import etree
        result = {}
        self.Guard._walk(etree.fromstring(arch), model, False, result)
        return result.get(model, set())

    def test_only_displayed_fields_count(self):
        seen = self._walk(
            '<form><field name="name"/>'
            '<field name="ref" invisible="1"/>'
            '<field name="comment" invisible="True"/>'
            '<group invisible="1"><field name="website"/></group>'
            '<field name="email" invisible="not is_company"/>'
            '<list><field name="phone" column_invisible="True"/></list>'
            '</form>')
        self.assertIn('name', seen)
        self.assertIn('email', seen, 'a conditionally hidden field can show')
        for hidden in ('ref', 'comment', 'website', 'phone'):
            self.assertNotIn(hidden, seen)

    def test_subview_fields_belong_to_the_comodel(self):
        from lxml import etree
        result = {}
        self.Guard._walk(etree.fromstring(
            '<form><field name="child_ids"><list><field name="email"/>'
            '</list></field></form>'), 'res.partner', False, result)
        self.assertIn('child_ids', result['res.partner'])
        self.assertIn('email', result['res.partner'])  # same comodel here

    def test_sensitive_fields_are_never_allowed(self):
        for name in ('password', 'api_key', 'acc_number', 'iban',
                     'identification_id', 'ssnid', 'passport_id',
                     'private_email', 'birthday', 'bank_account_id'):
            with self.assertRaises(ToolInputError, msg=name):
                self.Guard._check_field('res.partner', name)

    def test_sensitive_and_technical_models_are_blocked(self):
        for model in ('res.partner.bank', 'res.users', 'res.users.apikeys',
                      'ir.config_parameter', 'ir.attachment',
                      'resource.resource', 'ksw.ai.turn'):
            self.assertTrue(self.Guard._model_blocked(model), model)
        self.assertFalse(self.Guard._model_blocked('res.partner'))

    def test_hidden_field_refused_in_every_position(self):
        """A field off the user's screens can be neither shown, filtered,
        sorted, grouped nor summed."""
        tools = self.Tools.with_user(self.user)
        allowed = self.Guard.with_user(self.user)._allowed('res.partner')
        hidden = next(f for f in ('barcode_sanitized', 'signup_type',
                                  'partner_share', 'commercial_company_name',
                                  'partner_gid')
                      if f in self.env['res.partner']._fields
                      and f not in allowed)
        base = {'model': 'res.partner', 'order': '', 'limit': 3,
                'title': 'x'}
        attempts = [
            ('search_records', dict(base, domain='[]', fields=[hidden])),
            ('search_records', dict(base, domain=json.dumps(
                [[hidden, '!=', False]]), fields=['name'])),
            ('search_records', dict(base, domain='[]', fields=['name'],
                                    order='%s desc' % hidden)),
            ('search_records', dict(base, domain=json.dumps(
                [['parent_id', 'any', [[hidden, '!=', False]]]]),
                fields=['name'])),
            ('search_records', dict(base, domain=json.dumps(
                [['parent_id.%s' % hidden, '!=', False]]), fields=['name'])),
            ('summarize_records', {'model': 'res.partner', 'domain': '[]',
                                   'group_by': [hidden], 'measures': [],
                                   'title': 'x'}),
        ]
        for name, args in attempts:
            with self.assertRaises(ToolInputError, msg=str(args)):
                tools._run_tool(name, args)

    def test_order_cannot_carry_sql(self):
        with self.assertRaises(ToolInputError):
            self.Tools._run_tool('search_records', {
                'model': 'res.partner', 'domain': '[]', 'fields': ['name'],
                'order': 'id desc; select 1', 'limit': 3, 'title': 'x'})

    def test_read_record_drops_hidden_and_sensitive_fields(self):
        partner = self.env['res.partner'].create({
            'name': 'AI Sec Co', 'barcode': 'SECRET-BARCODE'})
        result, _b = self.Tools.with_user(self.user)._run_tool(
            'read_record', {'model': 'res.partner',
                            'record_id': partner.id})
        dump = json.dumps(result, default=str)
        self.assertNotIn('SECRET-BARCODE', dump)
        allowed = self.Guard.with_user(self.user)._allowed('res.partner')
        self.assertTrue(set(result['fields']) <= allowed)

    def test_pay_of_others_needs_a_payroll_role(self):
        plain = self.Guard.with_user(self.user)
        self.assertFalse(plain._pay_privileged())
        self.assertEqual(plain._pay_scope('hr.payslip', []),
                         [('employee_id.user_id', '=', self.user.id)])
        self.assertEqual(plain._pay_scope('hr.leave', ['x_eos_last_wage']),
                         [('employee_id.user_id', '=', self.user.id)])
        self.assertEqual(plain._pay_scope('hr.leave', ['state']), [])
        with self.assertRaises(ToolInputError):
            plain._pay_scope('hr.payslip.run', [])
        officer = self.env['res.users'].create({
            'name': 'AI Sec Officer', 'login': 'ai_sec_officer',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('om_hr_payroll.group_hr_payroll_user').id])]})
        self.assertEqual(
            self.Guard.with_user(officer)._pay_scope('hr.payslip', []), [])

    def test_portal_user_cannot_ask(self):
        portal = self.env['res.users'].create({
            'name': 'AI Portal', 'login': 'ai_portal',
            'group_ids': [(6, 0, [self.env.ref('base.group_portal').id])]})
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env['ksw.ai.conversation'].with_user(portal).ksw_ai_ask('x')
        self.assertFalse(self.env['ksw.ai.conversation'].with_user(
            portal).ksw_ai_status()['enabled'])

    def test_daily_limit(self):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('ksw_ai.api_key', 'test-key')
        ICP.set_param('ksw_ai.provider', 'anthropic')
        ICP.set_param('ksw_ai.daily_limit', '2')
        Conv = self.env['ksw.ai.conversation'].with_user(self.user)

        def answer(*args, **kwargs):
            return _reply([{'type': 'text', 'text': 'ok'}], 'end_turn')

        with patch.object(ai_conversation, 'create_message', answer):
            Conv.ksw_ai_ask('one')
            Conv.ksw_ai_ask('two')
            with self.assertRaises(UserError):
                Conv.ksw_ai_ask('three')

    def test_tool_results_are_marked_untrusted(self):
        state = {'blocks': [], 'calls': [], 'usage': {}}
        content, is_error = self.env['ksw.ai.conversation']._ksw_run_tool(
            self.Tools, 'find_models', {'keywords': []}, state)
        self.assertFalse(is_error)
        self.assertIn('untrusted_data', json.loads(content))
