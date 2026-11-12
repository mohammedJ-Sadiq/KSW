from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ksw_ai_provider = fields.Selection(
        [('groq', 'Groq (free tier)'),
         ('ollama', 'Ollama (own server, free, data stays in-house)'),
         ('anthropic', 'Anthropic Claude (paid)'),
         ('openai_compatible', 'Other OpenAI-compatible server')],
        string='AI Provider', config_parameter='ksw_ai.provider',
        default='groq')
    ksw_ai_api_key = fields.Char(
        string='API Key', config_parameter='ksw_ai.api_key',
        help='Groq: console.groq.com › API Keys. Claude: '
             'console.anthropic.com. Not needed for Ollama.')
    ksw_ai_model = fields.Char(
        string='Model', config_parameter='ksw_ai.model',
        help='Empty uses the provider default: openai/gpt-oss-120b on '
             'Groq, qwen3:8b on Ollama, claude-opus-5 on Anthropic.')
    ksw_ai_base_url = fields.Char(
        string='Server Address', config_parameter='ksw_ai.base_url',
        help='Only for Ollama or another server, e.g. '
             'http://192.168.1.50:11434/v1. Empty uses the default.')
    ksw_ai_effort = fields.Selection(
        [('low', 'Low (fastest)'), ('medium', 'Medium'),
         ('high', 'High (most careful)')],
        string='Reasoning Effort', config_parameter='ksw_ai.effort',
        default='high',
        help='How long the model thinks before answering. Higher is more '
             'accurate and slower.')

    @api.onchange('ksw_ai_provider')
    def _onchange_ksw_ai_provider(self):
        # A model name from one provider means nothing to another.
        self.ksw_ai_model = False
        self.ksw_ai_base_url = False
