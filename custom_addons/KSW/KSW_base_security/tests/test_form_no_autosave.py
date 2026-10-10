"""Browser test: leaving an unsaved time-off form asks instead of saving.

Stock Odoo saves (for time off: submits) a dirty form the moment the user
navigates away. `static/src/js/form_no_autosave.js` replaces that with a
"leave without saving?" dialog. Only a real browser can see it, so this
drives the client.

Needs the project venv (`browser_js` requires `websocket-client`):

    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 --test-enable \\
      --test-tags /KSW_base_security:TestFormNoAutosave -u KSW_base_security \\
      --stop-after-init
"""
from odoo.tests import HttpCase, tagged

_SCRIPT = """
(async () => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const waitFor = async (sel) => {
        for (let i = 0; i < 40; i++) {
            const el = document.querySelector(sel);
            if (el) { return el; }
            await sleep(250);
        }
        throw new Error('timed out waiting for ' + sel);
    };
    for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) { await sleep(250); }
    const env = odoo.__WOWL_DEBUG__.root.env;
    const count = () => env.services.orm.searchCount(
        'hr.leave', [['name', '=', %(marker)r]]);

    const openDirtyForm = async () => {
        await env.services.action.doAction({
            type: 'ir.actions.act_window', res_model: 'hr.leave',
            views: [[false, 'form']], target: 'current',
        });
        const input = await waitFor(
            '.o_form_view .o_field_widget[name="name"] textarea');
        input.value = %(marker)r;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        await sleep(500);
    };
    const leave = () => env.services.action.doAction({
        type: 'ir.actions.act_window', res_model: 'res.partner',
        views: [[false, 'list']], target: 'current',
    });
    const dialogButton = async (label) => {
        await waitFor('.modal .modal-footer button');
        const btn = [...document.querySelectorAll('.modal .modal-footer button')]
            .find((b) => b.textContent.trim() === label);
        if (!btn) { throw new Error('no "' + label + '" button in the dialog'); }
        btn.click();
        await sleep(800);
    };

    // 1. Stay here: still on the form, nothing saved.
    await openDirtyForm();
    const navStay = leave();
    await dialogButton('Stay here');
    await navStay;
    if (!document.querySelector('.o_form_view .o_field_widget[name="name"]')) {
        throw new Error('"Stay here" left the form');
    }
    if (await count()) { throw new Error('"Stay here" saved the request'); }

    // 2. Discard changes: navigation proceeds, nothing saved.
    const navDiscard = leave();
    await dialogButton('Discard changes');
    await navDiscard;
    await waitFor('.o_list_view');
    if (await count()) { throw new Error('"Discard changes" saved the request'); }

    // 3. An untouched form leaves without asking.
    await env.services.action.doAction({
        type: 'ir.actions.act_window', res_model: 'hr.leave',
        views: [[false, 'form']], target: 'current',
    });
    await waitFor('.o_form_view .o_field_widget[name="name"]');
    await leave();
    await waitFor('.o_list_view');
    if (document.querySelector('.modal')) {
        throw new Error('a prompt was shown for a form with no changes');
    }
    console.log('test successful');
})().catch((e) => console.error(e.message));
"""


@tagged('post_install', '-at_install')
class TestFormNoAutosave(HttpCase):

    def test_leaving_dirty_leave_form_prompts(self):
        marker = 'KSW no-autosave probe'
        self.browser_js(
            '/odoo/time-off', _SCRIPT % {'marker': marker},
            ready='odoo.isReady === true', login='admin', timeout=120,
        )
        self.assertFalse(
            self.env['hr.leave'].search_count([('name', '=', marker)]))
