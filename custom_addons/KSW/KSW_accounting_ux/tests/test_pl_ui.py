from datetime import date

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestPlUi(HttpCase):
    def test_pl_menu_opens_report(self):
        """Menu -> period dialog -> Show -> the report table renders."""
        code = """
            const t0 = Date.now();
            let clicked = false;
            const tick = () => {
                const btn = document.querySelector('.modal button[name="action_open"]');
                if (!clicked && btn) { btn.click(); clicked = true; }
                if (document.querySelector('.oe_mis_builder_content table td')) {
                    console.log('test successful');
                } else if (Date.now() - t0 > 20000) {
                    console.error('MIS table never rendered (dialog clicked: ' + clicked + ')');
                } else { setTimeout(tick, 300); }
            };
            tick();
        """
        self.browser_js('/odoo/action-KSW_accounting_ux.action_ksw_pl_wizard',
                        code, login='admin', timeout=60)

    def test_monthly_columns_cover_range(self):
        wiz = self.env['ksw.pl.wizard'].create({
            'date_from': date(2026, 1, 15), 'date_to': date(2026, 3, 10),
            'layout': 'monthly'})
        cols = wiz._periods()
        self.assertEqual([(c['manual_date_from'], c['manual_date_to']) for c in cols], [
            (date(2026, 1, 15), date(2026, 1, 31)),
            (date(2026, 2, 1), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 3, 10)),
            (date(2026, 1, 15), date(2026, 3, 10)),   # total column
        ])
