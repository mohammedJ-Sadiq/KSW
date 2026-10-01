"""The Who Gets Paid review, driven in a real browser.

The dialog's behaviour lives in the web client: the sort and filter bar runs
an onchange, Next saves the session before calling the server, and each step
must replace the dialog rather than close it (a falsy button result closes
it). None of that is visible from the ORM, so it is checked here in Chrome.
"""
from odoo.tests import HttpCase, tagged

from .test_submission import SubmissionCommon

JS = r"""
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(selector, test, label) {
    for (let i = 0; i < 150; i++) {
        const el = document.querySelector(selector);
        if (el && (!test || test(el))) { return el; }
        await sleep(100);
    }
    const el = document.querySelector(selector);
    throw new Error("timeout: " + (label || selector) + " | saw: " +
        (el ? (el.value || el.textContent).trim() : "nothing"));
}
const posIs = (txt) => waitFor(".modal div[name='position']",
    (el) => el.textContent.trim() === txt, "position " + txt);
const showing = (name) => waitFor(".modal div[name='employee_id']",
    (el) => el.textContent.trim() === name, "showing " + name);
const typeIn = (input, value) => {
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
};
(async () => {
    try {
        await waitFor("a.nav-link", (a) => [...document.querySelectorAll(
            "a.nav-link")].some((x) => x.textContent.includes("Who Gets Paid")));
        [...document.querySelectorAll("a.nav-link")].find(
            (a) => a.textContent.includes("Who Gets Paid")).click();
        (await waitFor("button[name='action_review_employees']")).click();

        // Default: department, then name.
        await posIs("1 / 3");
        await showing("Sub Emp A");
        await waitFor(".modal .o_ksw_employee_review", null, "sections");
        (await waitFor(".modal button[name='action_next']")).click();
        await posIs("2 / 3");
        await showing("Sub Emp A2");
        (await waitFor(".modal button[name='action_prev']")).click();
        await showing("Sub Emp A");

        // A new sort starts at its own top: the biggest earner, A2.
        (await waitFor(".modal div[name='sort_by'] .o_select_menu_toggler")).click();
        await waitFor(".o_select_menu_item", () => [...document.querySelectorAll(
            ".o_select_menu_item")].some((i) => i.textContent.includes(
                "Earnings, highest first")), "sort menu");
        [...document.querySelectorAll(".o_select_menu_item")].find(
            (i) => i.textContent.includes("Earnings, highest first")).click();
        await showing("Sub Emp A2");
        await posIs("1 / 3");

        // Department filter through the tags widget: only B is left.
        const tags = await waitFor(".modal div[name='department_ids'] input");
        tags.focus();
        typeIn(tags, "Sub Dept B");
        const item = await waitFor(".o-autocomplete--dropdown-item",
            () => [...document.querySelectorAll(".o-autocomplete--dropdown-item")]
                .some((i) => i.textContent.includes("Sub Dept B")), "dept option");
        const opt = [...document.querySelectorAll(".o-autocomplete--dropdown-item")]
            .find((i) => i.textContent.includes("Sub Dept B"));
        (opt.querySelector("a") || opt).click();
        await showing("Sub Emp B");
        await posIs("1 / 1");

        // A search with no match, then cleared: back to the B filter.
        const input = await waitFor(".modal div[name='search_text'] input");
        typeIn(input, "nobody at all");
        await posIs("0 / 0");
        await waitFor(".modal .alert-info", null, "no match message");
        typeIn(input, "");
        await posIs("1 / 1");
        await showing("Sub Emp B");

        [...document.querySelectorAll(".modal footer button")].find(
            (b) => b.textContent.trim() === "Close").click();
        for (let i = 0; i < 50 && document.querySelector(".modal"); i++) {
            await sleep(100);
        }
        if (document.querySelector(".modal")) {
            throw new Error("Close left the dialog open");
        }
        console.log("test successful");
    } catch (e) {
        console.error("review UI: " + e.message);
    }
})();
"""


@tagged('post_install', '-at_install')
class TestRunReviewUI(HttpCase, SubmissionCommon):

    def test_review_dialog_walks_sorts_and_filters(self):
        emp_a2 = self._employee('Sub Emp A2', self.dept_a, 7200.0)
        a = self._batch(self.sup_a, self.dept_a)
        self._entry(a, self.emp_a, user=self.sup_a, quantity=4.0)
        # The biggest earner, so sorting by earnings visibly moves him up.
        self._entry(a, emp_a2, user=self.sup_a, quantity=40.0)
        meals = self._batch(self.sup_a, self.dept_a, component=self.meals)
        self._entry(meals, self.emp_a, user=self.sup_a, quantity=5.0)
        b = self._filled_batch(self.sup_b, self.dept_b, self.emp_b)
        a.submission_id.with_user(self.sup_a).action_submit()
        b.submission_id.with_user(self.sup_b).action_submit()
        run = a.submission_id.run_id
        self.gm.sudo().password = 'sub_gm'
        self.browser_js(
            '/odoo/ksw.pay.run/%s' % run.id, JS, login='sub_gm',
            timeout=120)
