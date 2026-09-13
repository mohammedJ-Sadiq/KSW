import { patch } from "@web/core/utils/patch";
import { FileModel } from "@web/core/file_viewer/file_model";
import { useFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { Many2ManyBinaryField } from "@web/views/fields/many2many_binary/many2many_binary_field";

/**
 * Open attachments instead of only downloading them.
 *
 * `web.Many2ManyBinaryField` renders every tile as three `<a download="">`
 * links pointing at `/web/content/<id>?download=true`, so the only way to look
 * at a signed leave form, an ID copy or a loan agreement is to save it to disk
 * first and open it from there.
 *
 * Odoo already has the component for this — `FileViewer`, the overlay the
 * chatter uses: PDFs through pdf.js, images inline, with **Print** and
 * **Download** in its toolbar and arrows to page through the other
 * attachments on the record. This just wires the widget to it.
 *
 * Files the viewer cannot render (.xlsx, .docx, .zip) are left alone: the
 * handler returns without `preventDefault()` and the anchor downloads as
 * before.
 */
patch(Many2ManyBinaryField.prototype, {
    setup() {
        super.setup(...arguments);
        this.fileViewer = useFileViewer();
    },

    /**
     * `this.files` are plain dicts read off the x2many records. FileViewer
     * needs the model that carries `isViewable` / `defaultSource` /
     * `downloadUrl`.
     */
    toFileModel(file) {
        return Object.assign(new FileModel(), file);
    },

    isViewable(file) {
        return this.toFileModel(file).isViewable;
    },

    onClickAttachment(file, ev) {
        const models = this.files.map((f) => this.toFileModel(f));
        const clicked = models.find((model) => model.id === file.id);
        if (!clicked || !clicked.isViewable) {
            // Let the anchor's own download happen.
            return;
        }
        ev.preventDefault();
        this.fileViewer.open(clicked, models);
    },
});
