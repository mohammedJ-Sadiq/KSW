{
    'name': 'KSW - AI Assistant',
    'category': 'Productivity',
    'version': '19.0.1.1.0',
    'author': 'Mohammed Albadr',
    'summary': 'Ask Odoo questions in plain Arabic or English, from Home or '
               'any screen',
    'description': """
AI Assistant
============
An "Ask" box on the KSW Home page and an assistant button in the top bar.
Any user can ask, in Arabic or English, things like "show me all loans
above 5,000 SAR" or "how many employees are on annual leave this week?".

* Answers come from a language model, which reads Odoo through four
  read-only tools: find the screen, list its fields, search records,
  total and group records. Provider in Settings: Groq (free tier,
  default), Ollama on your own server (free, data stays in-house),
  Anthropic Claude, or any OpenAI-compatible server.
* Every tool runs as the asking user, so the assistant sees exactly
  what that user's access rights allow, no more.
* It never creates, changes, approves or deletes anything.
* Every answer shows the filter it actually ran and an "Open in Odoo"
  button for the same list, so the reader can check it.
* Each question is logged (who asked, what ran, tokens used).

Set it up in Settings > General Settings > AI Assistant.
    """,
    'license': 'LGPL-3',
    'depends': ['base_setup', 'web', 'mail', 'KSW_backend_theme'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'views/ai_conversation_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'KSW_ai_assistant/static/src/chat/*.js',
            'KSW_ai_assistant/static/src/chat/*.xml',
            'KSW_ai_assistant/static/src/chat/*.scss',
        ],
    },
    'installable': True,
    'application': False,
}
