{
    'name': 'KSW - Backend Theme',
    'version': '19.0.4.1.0',
    'author': 'Mohammed Albadr',
    'summary': 'KSW look for the MuK backend: company colours, light top bar, Arabic font',
    'description': """
A visual layer on top of MuK Backend Theme; it changes no behaviour.

* Company colours (navy #124081, teal #0b7899, the report colours darkened
  to pass 4.5:1 text contrast) replace MuK's stock slate.
* Light top bar and apps sidebar, the Enterprise 17+ layout, instead of
  two dark bars around the page.
* Plain apps screen instead of a photo; rounded cards, sheets and buttons.
* Neutral chatter buttons: MuK paints "Send message" red (btn-danger).
* Tajawal (shipped by core web, OFL) for Arabic characters only.
* A 4-step quick tour (Home, search, appearance, text size), once per
  account, replayable from the user menu.
* Dark mode (user menu: Light / Dark / Automatic, per device): a full dark
  palette, since Community ships the dark bundle without one.
* Arabic for MuK's own strings and preference options, which ship none.
* Home, the landing page and the one app switcher: what is waiting for me
  (configurable counters, only non-zero shown, counted under the user's own
  access rights), my apps (personal hide / reorder, admin default for everyone),
  recently opened records, and a visible search over every app and screen.
* One primary action per screen, actions that stay visible while scrolling,
  status as pills, a visible search button (Ctrl+K) in the top bar.

Supersedes MuK's Settings colour pickers: the colours live in
static/src/scss/primary_variables.scss. Uninstall to get MuK back as it was.
    """,
    'license': 'LGPL-3',
    'category': 'Themes/Backend',
    'depends': ['muk_web_theme', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/home_views.xml',
        'views/webclient_templates.xml',
        'data/home_inbox_sources.xml',
    ],
    'assets': {
        'web._assets_primary_variables': [
            # After MuK's colour files: they assign without !default.
            ('after', 'muk_web_colors/static/src/scss/colors.scss',
             'KSW_backend_theme/static/src/scss/primary_variables.scss'),
            ('after', 'muk_web_theme/static/src/scss/colors.scss',
             'KSW_backend_theme/static/src/scss/appsbar_variables.scss'),
        ],
        # Dark mode: right after the light tokens, so these win (Community has no
        # global dark palette of its own). The lazy bundle carries graph and pivot.
        'web.assets_web_dark': [
            ('after', 'KSW_backend_theme/static/src/scss/primary_variables.scss',
             'KSW_backend_theme/static/src/scss/primary_variables.dark.scss'),
        ],
        'web.assets_backend_lazy_dark': [
            ('after', 'KSW_backend_theme/static/src/scss/primary_variables.scss',
             'KSW_backend_theme/static/src/scss/primary_variables.dark.scss'),
        ],
        'web.assets_frontend': [
            'KSW_backend_theme/static/src/scss/login.scss',
        ],
        'web.assets_backend': [
            'KSW_backend_theme/static/src/appearance/appearance.js',
            'KSW_backend_theme/static/src/scss/theme.scss',
            'KSW_backend_theme/static/src/home/shell.js',
            'KSW_backend_theme/static/src/home/home.js',
            'KSW_backend_theme/static/src/home/home.xml',
            'KSW_backend_theme/static/src/home/home.scss',
            'KSW_backend_theme/static/src/tour/quick_tour.js',
            'KSW_backend_theme/static/src/tour/quick_tour.xml',
            'KSW_backend_theme/static/src/tour/quick_tour.scss',
        ],
    },
    'installable': True,
    'application': False,
}
