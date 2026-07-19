import base64
import logging
from io import BytesIO

from markupsafe import Markup
from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'bmp', 'tiff', 'tif', 'webp', 'gif'}


def _compress_file(data_b64, filename):
    """
    Re-encode image uploads as JPEG at 85% quality to reduce storage size.
    Non-image files (PDFs etc.) are returned unchanged — their internal
    streams are already compressed and zlib-wrapping would corrupt the format.
    Returns (data_b64, filename).
    """
    filename = filename or ''
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

    if ext not in _IMAGE_EXTENSIONS:
        return data_b64, filename

    try:
        from PIL import Image
        raw = base64.b64decode(data_b64)
        img = Image.open(BytesIO(raw))

        # JPEG does not support alpha; flatten to white background
        if img.mode in ('RGBA', 'P', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        out = BytesIO()
        img.save(out, format='JPEG', quality=85, optimize=True)
        compressed_b64 = base64.b64encode(out.getvalue()).decode()

        base_name = filename.rsplit('.', 1)[0] if '.' in filename else filename
        new_filename = base_name + '.jpg'

        original_kb = len(base64.b64decode(data_b64)) // 1024
        compressed_kb = len(out.getvalue()) // 1024
        _logger.info(
            'KSW attachment compression: %s → %s (%d KB → %d KB)',
            filename, new_filename, original_kb, compressed_kb,
        )
        return compressed_b64, new_filename

    except Exception:
        _logger.warning('KSW attachment compression failed for %s, storing original', filename, exc_info=True)
        return data_b64, filename


class KswHrConfirmSignatureWizard(models.TransientModel):
    _name = 'ksw.hr.confirm.signature.wizard'
    _description = 'HR: Confirm Signed Vacation Form'

    leave_id = fields.Many2one('hr.leave', required=True, ondelete='cascade')
    employee_name = fields.Char(related='leave_id.employee_id.name', readonly=True)
    leave_period = fields.Char(compute='_compute_leave_period', readonly=True)

    attachment_file = fields.Binary(string='Signed Vacation Form', required=True)
    attachment_filename = fields.Char(string='File Name')

    def _compute_leave_period(self):
        for wiz in self:
            leave = wiz.leave_id
            if leave.request_date_from and leave.request_date_to:
                wiz.leave_period = '%s → %s (%g days)' % (
                    leave.request_date_from,
                    leave.request_date_to,
                    leave.number_of_days,
                )
            else:
                wiz.leave_period = ''

    def action_confirm(self):
        self.ensure_one()
        user = self.env.user
        leave = self.leave_id

        if not self.env.su and not user.has_group('KSW_annual_leave.group_annual_leave_hr'):
            raise UserError('Only an HR Approver can confirm the document upload.')

        if leave.x_annual_approval_state != 'pending_employee_signature':
            raise UserError('This leave is not pending HR confirmation.')

        data_b64, filename = _compress_file(
            self.attachment_file,
            self.attachment_filename or 'signed_vacation_form',
        )

        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': data_b64,
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, attachment.id)]})
        leave.with_user(user).sudo().action_employee_confirm_signature()

        return {'type': 'ir.actions.act_window_close'}
