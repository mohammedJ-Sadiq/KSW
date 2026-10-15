"""Export complete translation templates for every installed KSW module.

`odoo-bin i18n export` misses every Python `_()` / `_lt()` string and every
JS `_t()` / OWL template string in KSW modules: addons_path lists
`custom_addons` BEFORE `custom_addons/KSW`, so TranslationModuleReader maps
each file to a module literally named "KSW", which is not installed, and
skips it. This runs Odoo's own exporter with the KSW directory scanned first.

Writes <OUT>/<module>.pot (template) and <OUT>/<module>.po (current ar_001
values in the DB). Run through the Odoo shell:

    OUT=/path/to/dir python odoo-bin shell -c KSW_dev.conf --http-port=18072 \
        --no-http < custom_addons/KSW/_export_ksw_terms.py

Each code string is listed twice (`code:addons/KSW_x/...` and
`code:addons/KSW/KSW_x/...`); drop the second form before writing a module's
.pot. A .po code entry needs the `#. odoo-python` / `#. odoo-javascript`
comment this exporter writes, or Odoo ignores it at runtime.
"""
import io
import os

from odoo.tools import translate as T

OUT = os.environ['OUT']
KSW = os.path.dirname(os.path.abspath(__file__))


def _init(self, cr, modules=None, lang=None):
    # Same as core, with the KSW directory scanned first.
    T.TranslationReader.__init__(self, cr, lang)
    self._modules = modules or ['all']
    import odoo.addons
    self._path_list = [(KSW, True)] + [(p, True) for p in odoo.addons.__path__]
    self._installed_modules = self.env['ir.module.module'].search(
        [('state', '=', 'installed')]).mapped('name')
    self._export_translatable_records()
    self._export_translatable_resources()


T.TranslationModuleReader.__init__ = _init
os.makedirs(OUT, exist_ok=True)
for module in env['ir.module.module'].search(  # noqa: F821 (odoo shell)
        [('name', '=like', 'KSW%'), ('state', '=', 'installed')]).mapped('name'):
    for lang, ext in ((None, 'pot'), ('ar_001', 'po')):
        buf = io.BytesIO()
        T.trans_export(lang, [module], buf, 'po', env)  # noqa: F821
        with open(os.path.join(OUT, f'{module}.{ext}'), 'wb') as fh:
            fh.write(buf.getvalue())
    print('exported', module)
