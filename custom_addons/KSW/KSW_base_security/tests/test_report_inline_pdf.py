import json
from urllib.parse import unquote

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestInlinePdfDisposition(HttpCase):
    """`/report/pdf/...` must name the PDF it streams.

    KSW reports are opened straight in a browser tab instead of being
    downloaded (see `static/src/js/report_open_inline.js`). The stock route
    sends no Content-Disposition at all, so the viewer's Save button would
    offer `<record id>.pdf`; the controller override gives it the same name
    `/report/download` uses, as `inline` so the tab still renders it.

    The report below is created by the test rather than borrowing a real one:
    the behaviour under test belongs to the route, not to any one document.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'Inline Report Subject'})
        cls.env['ir.ui.view'].create({
            'name': 'ksw_inline_pdf_test',
            'type': 'qweb',
            'key': 'KSW_base_security.ksw_inline_pdf_test',
            'arch': '''
                <t t-name="KSW_base_security.ksw_inline_pdf_test">
                    <t t-call="web.html_container">
                        <t t-foreach="docs" t-as="o">
                            <div class="page"><span t-out="o.name"/></div>
                        </t>
                    </t>
                </t>
            ''',
        })
        cls.report = cls.env['ir.actions.report'].create({
            'name': 'KSW Inline PDF Test',
            'model': 'res.partner',
            'report_type': 'qweb-pdf',
            'report_name': 'KSW_base_security.ksw_inline_pdf_test',
            'report_file': 'KSW_base_security.ksw_inline_pdf_test',
            'print_report_name': "'Inline Test - %s' % (object.name)",
        })
        cls.env['res.users'].create({
            'name': 'Inline Report Tester',
            'login': 'ksw_inline_tester',
            'password': 'ksw_inline_tester',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    def _pdf_url(self):
        return '/report/pdf/%s/%s' % (self.report.report_name, self.partner.id)

    def test_pdf_route_is_inline_and_named(self):
        self.authenticate('ksw_inline_tester', 'ksw_inline_tester')
        res = self.url_open(self._pdf_url())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers['Content-Type'], 'application/pdf')
        disposition = res.headers.get('Content-Disposition', '')
        self.assertTrue(
            disposition.startswith('inline'),
            'the PDF must render in the tab, not download: %r' % disposition)
        # RFC 6266: the name travels percent-encoded in filename*.
        self.assertIn(
            'Inline Test - Inline Report Subject.pdf', unquote(disposition))

    def test_download_route_keeps_a_single_attachment_header(self):
        """`/report/download` calls `report_routes` itself and adds its own
        header afterwards; the override must not leave a second one behind."""
        self.authenticate('ksw_inline_tester', 'ksw_inline_tester')
        res = self.url_open('/report/download?data=%s&context=%s' % (
            json.dumps([self._pdf_url(), 'qweb-pdf']), '{}'))
        self.assertEqual(res.status_code, 200)
        # requests joins repeated headers with ', ', so a duplicate would show
        # up as two filenames in the one value.
        disposition = res.headers.get('Content-Disposition', '')
        self.assertTrue(disposition.startswith('attachment'), disposition)
        self.assertEqual(disposition.count('filename'), 1, disposition)

    def test_backend_bundle_loads_both_patches(self):
        """Smoke test for the whole asset contribution.

        Two distinct failure modes are covered by booting the client at all:
        a bad *import* in either JS file breaks the backend bundle, and a bad
        *xpath* in `attachment_preview.xml` makes the server fail to build the
        bundle, so `/web/assets/....js` 500s and nothing renders. Then read
        both patches back out of the running client."""
        self.browser_js(
            '/odoo',
            """
            (async () => {
                const { registry } = odoo.loader.modules.get("@web/core/registry");
                const handlers = registry.category("ir.actions.report handlers");
                if (!handlers.contains("ksw_open_pdf_inline")) {
                    throw new Error("ksw_open_pdf_inline handler not registered");
                }
                const { Many2ManyBinaryField } = odoo.loader.modules.get(
                    "@web/views/fields/many2many_binary/many2many_binary_field");
                if (!Many2ManyBinaryField.prototype.onClickAttachment) {
                    throw new Error("Many2ManyBinaryField not patched for preview");
                }
                console.log("test successful");
            })();
            """,
            login='ksw_inline_tester',
        )
