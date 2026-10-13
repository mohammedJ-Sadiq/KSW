{
    'name': 'KSW - Mobile App Database List',
    'version': '19.0.1.0.0',
    'author': 'Mohammed Albadr',
    'summary': 'Let mobile apps discover the single served database while list_db stays off',
    'description': """
Mobile apps (e.g. Cybrosys Odoo Mobo) have no database field: they call
/web/database/list and give up when list_db = False refuses it.

This module answers that one route with the databases that dbfilter / db_name
already restrict the server to (in production: just KSWCO). The database
manager (/web/database/manager, backup, drop, duplicate) stays disabled.
    """,
    'license': 'LGPL-3',
    'depends': ['web'],
    'data': [],
    'installable': True,
    'auto_install': False,
    'application': False,
}
