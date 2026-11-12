"""OpenAI-compatible Chat Completions client (Groq, Ollama, others).

Groq's free tier, a local Ollama server and most other providers speak this
format, so one client covers them all. Raw HTTP with ``requests``, which
ships with Odoo.
"""
import logging
import time

import requests

from .claude_client import ClaudeError as ProviderError

_logger = logging.getLogger(__name__)

PRESETS = {
    'groq': {'base_url': 'https://api.groq.com/openai/v1',
             'model': 'openai/gpt-oss-120b'},
    'ollama': {'base_url': 'http://localhost:11434/v1',
               'model': 'qwen3:8b'},
}
TIMEOUT = 180  # a CPU-only Ollama is slow
# Groq's free tier allows 8,000 tokens a minute; one question can cross
# it. Waiting out a short "retry-after" beats failing the question.
MAX_WAIT = 30


def chat_completion(base_url, api_key, model, messages, tools,
                    max_tokens=4096, effort='high'):
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {'content-type': 'application/json'}
    if api_key:
        headers['authorization'] = 'Bearer %s' % api_key
    body = {
        'model': model,
        'messages': messages,
        'tools': tools,
        'tool_choice': 'auto',
        'max_tokens': max_tokens,
        'temperature': 0,
    }
    # Reasoning models answer far better when allowed to think before
    # choosing a tool ("it answers immediately" was the low default).
    if 'gpt-oss' in model:
        body['reasoning_effort'] = effort
        body['include_reasoning'] = False
    elif 'qwen3' in model and 'groq.com' in base_url:
        body['reasoning_effort'] = 'default'
        body['reasoning_format'] = 'hidden'
    for attempt in range(3):
        try:
            resp = requests.post(url, json=body, headers=headers,
                                 timeout=TIMEOUT)
        except requests.RequestException as exc:
            _logger.warning('AI provider unreachable (%s): %s', url, exc)
            raise ProviderError('connection') from exc
        if resp.status_code == 429 and attempt < 2:
            try:
                wait = float(resp.headers.get('retry-after') or 10)
            except ValueError:
                wait = 10
            if wait <= MAX_WAIT:
                time.sleep(wait)
                continue
        break
    if resp.status_code in (401, 403):
        raise ProviderError('auth')
    if resp.status_code == 429:
        raise ProviderError('rate_limit')
    if resp.status_code >= 400:
        _logger.warning('AI provider error %s: %s', resp.status_code,
                        resp.text[:2000])
        raise ProviderError('api')
    return resp.json()


def to_openai_tools(tools):
    """Our Anthropic-style tool list in the OpenAI ``functions`` shape."""
    return [{
        'type': 'function',
        'function': {
            'name': t['name'],
            'description': t['description'],
            'parameters': t['input_schema'],
        },
    } for t in tools]
