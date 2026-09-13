from odoo import http
from odoo.http import content_disposition, request
from odoo.tools.safe_eval import safe_eval, time

from odoo.addons.web.controllers.report import ReportController


class KswReportController(ReportController):
    """Give the inline PDFs a real file name.

    `/report/pdf/<report>/<ids>` returns the PDF with no `Content-Disposition`
    at all, so the browser's viewer offers to save it under the last segment of
    the URL -- `4839.pdf`. KSW reports are opened through that route on purpose
    (see `static/src/js/report_open_inline.js`), so stamp the response with the
    same name `/report/download` would have used, as `inline` so the PDF still
    renders in the tab rather than downloading.
    """

    @http.route()
    def report_routes(self, reportname, docids=None, converter=None, **data):
        response = super().report_routes(
            reportname, docids=docids, converter=converter, **data)
        # Only the direct route. `/report/download` calls this method itself
        # and adds its own `attachment` header afterwards; a second one here
        # would leave the response with two Content-Disposition headers.
        if converter == 'pdf' and request.httprequest.path.startswith('/report/pdf/'):
            filename = self._ksw_pdf_filename(reportname, docids)
            if filename:
                response.headers.add(
                    'Content-Disposition',
                    content_disposition(filename, disposition_type='inline'))
        return response

    def _ksw_pdf_filename(self, reportname, docids):
        """Same naming rule as `ReportController.report_download`."""
        report = request.env['ir.actions.report']._get_report_from_name(reportname)
        if not report:
            return None
        filename = '%s.pdf' % report.name
        ids = [int(x) for x in (docids or '').split(',') if x.isdigit()]
        if ids and report.model and report.print_report_name:
            records = request.env[report.model].browse(ids)
            if len(records) == 1:
                filename = '%s.pdf' % safe_eval(
                    report.print_report_name, {'object': records, 'time': time})
        return filename
