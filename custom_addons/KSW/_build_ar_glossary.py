#!/usr/bin/env python3
"""Generate custom_addons/KSW/ar_glossary_review.csv — the FULL master glossary.

Every unique short (non-sentence) source string across the 14 KSW modules gets a
row with its final proposed Arabic and a `status` column, so the reviewer sees
100% coverage — not just the deltas. Longer strings (help texts, tooltips,
report paragraphs) are handled automatically in `_translate_ksw_to_ar.py` and are
NOT listed here (user decision: no line-by-line review of prose).

Precedence engine + terminology decisions live in `_ar_engine.py`.
"""
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ar_engine as E  # noqa: E402

SCRATCH = '/tmp/claude-1000/-home-odoo-Odoo-odoo/79ddd4d6-bba6-45df-9dbd-afa35d8b04bc/scratchpad'
OUT = os.path.join(HERE, 'ar_glossary_review.csv')

ksw = json.load(open(f'{SCRATCH}/ksw_full.json'))
core = json.load(open(f'{SCRATCH}/core_ar.json'))


def is_sentence(s):
    plain = re.sub(r'<[^>]+>', '', s)
    return len(plain) > 60 or plain.rstrip().endswith(('.', '!', '?', ':')) or plain.count(' ') > 7


def has_text(s):
    return bool(re.sub(r'<[^>]+>', '', s).strip())


def mods_of(en):
    return ','.join(m.replace('KSW_', '') for m in ksw[en]['mods'])


# ── compute a proposed row for every short, text-bearing msgid ───────────────
records = []          # (en, current, proposed, status, source, note, section)
seen_proposed = {}    # en -> proposed  (consistency: one EN -> one AR)
for en, e in ksw.items():
    if is_sentence(en) or not has_text(en):
        continue
    proposed, status, source, note = E.propose(en, e['ar'], core)
    section = E.D_SECTION.get(en, {
        'changed': 'مصطلحات مُدارة', 'matched-core': 'عبارات النظام — مطابقة Odoo',
        'changed (auto)': 'تنظيف تلقائي (تشكيل/توحيد مفاهيم)', 'unchanged': 'تبقى كما هي',
    }.get(status, 'أخرى'))
    cur = ' | '.join(e['ar']) if e['ar'] else '(فارغ)'
    records.append((en, cur, proposed, status, source, note, section))
    seen_proposed[en] = proposed

# ── ordering: curated sections first, then core-matched, auto, unchanged ─────
STATUS_ORDER = {'changed': 0, 'matched-core': 1, 'changed (auto)': 2, 'unchanged': 3}
SEC_ORDER = {t: i for i, (t, _) in enumerate(E.SECTIONS)}


def sort_key(r):
    en, cur, proposed, status, source, note, section = r
    return (STATUS_ORDER.get(status, 9), SEC_ORDER.get(section, 99), en.lower())


records.sort(key=sort_key)

# ── write CSV grouped by (status, section) headers ───────────────────────────
with open(OUT, 'w', newline='', encoding='utf-8-sig') as f:
    f.write('# KSW Arabic — FULL master glossary / جدول المصطلحات العربية الكامل\n')
    f.write('# راجع عمود proposed_ar وعدّله مباشرة حيث تريد صيغة مختلفة، ثم أعد الملف.\n')
    f.write('# status: changed = قرار مُدار | matched-core = مطابق لترجمة Odoo الرسمية\n')
    f.write('#         changed (auto) = تنظيف تلقائي (إزالة تشكيل/توحيد مفهوم) | unchanged = يبقى كما هو\n')
    f.write('# source: labour-law/ksa-hr/sap-oracle/odoo-core/unify/auto/keep-current\n')
    f.write('# ملاحظة: العبارات الطويلة (تلميحات/مساعدة/رسائل) تُعالَج آلياً ولا تظهر هنا.\n')
    w = csv.writer(f)
    w.writerow(['term_en', 'current_ar', 'proposed_ar', 'status', 'source', 'modules', 'note'])
    last_hdr = None
    counts = {}
    for en, cur, proposed, status, source, note, section in records:
        counts[status] = counts.get(status, 0) + 1
        hdr = (status, section)
        if hdr != last_hdr:
            f.write(f'# ═══ [{status}] {section} ═══\n')
            last_hdr = hdr
        w.writerow([en, cur, proposed, status, source, mods_of(en), note])
    f.write('# ─── مصطلحات تبقى كما هي عمداً رغم اختلافها عن Odoo (للاطلاع فقط) ───\n')
    for en, reason in E.KEEP_CURRENT.items():
        if en in ksw:
            cur = ' | '.join(ksw[en]['ar'])
            f.write(f'# KEEP: {en} = {cur} — {reason}\n')

# ── consistency self-check ───────────────────────────────────────────────────
problems = []
for en, cur, proposed, status, source, note, section in records:
    if E._TASHKEEL.search(proposed):
        problems.append(f'TASHKEEL remains: {en!r} -> {proposed!r}')
    if status in ('changed (auto)', 'unchanged'):
        for tok in E.SUPERSEDED_TOKENS:
            if tok in proposed:
                problems.append(f'SUPERSEDED token {tok!r} in auto row: {en!r} -> {proposed!r}')

print(f'wrote {len(records)} short-term rows to {OUT}')
print('  by status: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
if problems:
    print(f'\n⚠ {len(problems)} consistency issue(s):')
    for p in problems[:40]:
        print('   ', p)
else:
    print('  consistency self-check: OK (no tashkeel, no superseded tokens in auto rows)')
