"""Set the train.* users' language (KSW_DEMO_LANG=en|ar) and switch the
guided tours off, so screenshots show the right language and no tour bubble.

    KSW_DEMO_LANG=ar python odoo-bin shell -c KSW_dev.conf --no-http < .../set_train_lang.py
"""
import os

lang = {'ar': 'ar_001'}.get(os.environ.get('KSW_DEMO_LANG', 'en'), 'en_US')
users = env['res.users'].search([('login', 'like', 'train.%')])
vals = {'lang': lang}
if 'tour_enabled' in users._fields:
    vals['tour_enabled'] = False
users.write(vals)
env.cr.commit()
print('TRAIN_LANG', lang, users.mapped('login'))
