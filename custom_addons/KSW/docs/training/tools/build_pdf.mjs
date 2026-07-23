/*
 * Build printable PDF handbooks from the training Markdown.
 *   node build_pdf.mjs            # both languages, full manual
 *   node build_pdf.mjs en         # one language
 *
 * Pipeline: Markdown -> HTML (marked) -> PDF (headless Chrome).
 * - Screenshots are resolved to absolute paths; a missing one becomes a labelled
 *   placeholder box (so the 12 not-yet-captured shots don't break the PDF).
 * - Arabic pages render RTL with Noto Naskh Arabic.
 * Output: ../pdf/KSW-User-Manual-<lang>.pdf
 */
import { marked } from 'marked';
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'fs';
import { dirname, join, resolve } from 'path';
import { fileURLToPath } from 'url';
import { execFileSync } from 'child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');            // docs/training
const OUT = join(ROOT, 'pdf');
mkdirSync(OUT, { recursive: true });

const PERSONAS = ['employee', 'supervisor', 'hr', 'accounting', 'gm', 'payroll', 'admin'];

function personaFiles(lang, persona) {
  const dir = join(ROOT, lang, persona);
  if (!existsSync(dir)) return [];
  const files = readdirSync(dir).filter(f => f.endsWith('.md'));
  files.sort((a, b) => (a === 'README.md' ? -1 : b === 'README.md' ? 1 : a.localeCompare(b)));
  return files.map(f => join(dir, f));
}

function orderedFiles(lang) {
  const list = [];
  const gs = join(ROOT, lang, '00-getting-started.md');
  if (existsSync(gs)) list.push(gs);
  for (const p of PERSONAS) list.push(...personaFiles(lang, p));
  return list;
}

// stable in-document anchor id for a guide file (relative to the lang root)
function sectionId(langRoot, mdPath) {
  return 'g-' + resolve(mdPath).slice(resolve(langRoot).length + 1)
    .replace(/\.md$/, '').replace(/[^a-zA-Z0-9]+/g, '-').toLowerCase();
}

// convert one md file to an HTML section: resolve images, and turn intra-manual
// .md links into internal anchors (real http links are kept as-is)
function sectionHtml(mdPath, langRoot, validIds) {
  let md = readFileSync(mdPath, 'utf8');
  md = md.replace(/^<div dir="rtl" markdown="1">\s*$/m, '').replace(/^<\/div>\s*$/m, '');
  const base = dirname(mdPath);
  let html = marked.parse(md);

  // images -> absolute path, or a labelled placeholder if not captured yet
  html = html.replace(/<img[^>]*alt="([^"]*)"[^>]*src="([^"]*)"[^>]*>|<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*>/g,
    (m, a1, s1, s2, a2) => {
      const src = s1 || s2 || '', alt = a1 || a2 || '';
      const abs = resolve(base, src);
      if (existsSync(abs)) return `<img class="shot" src="file://${abs}" alt="${alt}">`;
      return `<div class="ph">📷 ${alt || 'screenshot'} <span>(image pending capture)</span></div>`;
    });

  // links: keep http(s)/mailto; map .md refs to #anchor; otherwise drop the href
  html = html.replace(/<a href="([^"]+)">/g, (m, href) => {
    if (/^(https?:|mailto:)/i.test(href)) return m;
    const target = href.split('#')[0];
    if (target.endsWith('.md')) {
      const id = sectionId(langRoot, resolve(base, target));
      if (validIds.has(id)) return `<a href="#${id}">`;
    }
    return '<span class="xref">';   // no valid target -> render as plain styled text
  }).replace(/<\/a>/g, (m, idx, str) => m); // closing tags handled below
  // balance any <span class="xref"> we opened (replace their matching </a>)
  // simpler: re-close — marked pairs <a>..</a>; we converted some opens to spans.
  // Convert leftover </a> that follow a span-open by counting is fragile, so instead
  // do a second pass that pairs them.
  html = fixSpanCloses(html);

  return `<section id="${sectionId(langRoot, mdPath)}">${html}</section>`;
}

// turn "<span class="xref">text</a>" produced above into "<span ...>text</span>"
function fixSpanCloses(html) {
  const out = [];
  const re = /<span class="xref">|<a href="[^"]*">|<\/a>/g;
  let last = 0, m, stack = [];
  while ((m = re.exec(html))) {
    out.push(html.slice(last, m.index));
    if (m[0] === '</a>') {
      const open = stack.pop();
      out.push(open === 'span' ? '</span>' : '</a>');
    } else {
      stack.push(m[0].startsWith('<span') ? 'span' : 'a');
      out.push(m[0]);
    }
    last = re.lastIndex;
  }
  out.push(html.slice(last));
  return out.join('');
}

// persona -> [file-slug, English title, Arabic title]
const PERSONA_DOCS = {
  employee:   ['Employee',   'Employee Guide',         'دليل الموظف'],
  supervisor: ['Supervisor', 'Supervisor Guide',       'دليل المشرف'],
  hr:         ['HR',         'HR Guide',               'دليل الموارد البشرية'],
  accounting: ['Accounting', 'Accounting Guide',       'دليل المحاسبة'],
  gm:         ['GM',         'General Manager Guide',  'دليل المدير العام'],
  payroll:    ['Payroll',    'Payroll Guide',          'دليل الرواتب'],
  admin:      ['Admin',      'Administrator Guide',    'دليل المسؤولين'],
};

// render a set of markdown files into one PDF
function renderDoc(lang, files, outBase, title, subtitle) {
  const rtl = lang === 'ar';
  const langRoot = join(ROOT, lang);
  const validIds = new Set(files.map(f => sectionId(langRoot, f)));
  const body = files.map(f => sectionHtml(f, langRoot, validIds)).join('\n');
  const fontStack = rtl
    ? '"Noto Naskh Arabic","Noto Sans Arabic UI","DejaVu Sans",sans-serif'
    : '"DejaVu Sans",Arial,sans-serif';
  const html = `<!doctype html><html lang="${lang}" dir="${rtl ? 'rtl' : 'ltr'}"><head>
<meta charset="utf-8"><title>${title}</title>
<style>
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: ${fontStack}; font-size: 11pt; line-height: 1.55; color: #222; }
  h1 { font-size: 20pt; color: #0b5; border-bottom: 2px solid #0b5; padding-bottom: 4px;
       page-break-before: always; margin-top: 0; }
  section:first-child h1 { page-break-before: avoid; }
  h2 { font-size: 15pt; color: #146; margin-top: 1.2em; }
  h3 { font-size: 12.5pt; color: #333; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt; }
  th, td { border: 1px solid #ccc; padding: 5px 8px; text-align: ${rtl ? 'right' : 'left'}; }
  th { background: #eef6f0; }
  code { background: #f3f3f3; padding: 1px 4px; border-radius: 3px; font-family: "DejaVu Sans Mono",monospace; }
  pre { background: #f6f8fa; padding: 10px; border-radius: 5px; overflow-x: auto; direction: ltr; text-align: left; }
  blockquote { border-${rtl ? 'right' : 'left'}: 4px solid #0b5; margin: 8px 0; padding: 2px 12px; color: #444; background:#f7fbf8; }
  img.shot { max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin: 8px 0; display:block; page-break-inside: avoid; }
  .ph { border: 1px dashed #bbb; background: #fafafa; color: #888; padding: 14px; border-radius: 4px;
        margin: 8px 0; font-style: italic; text-align: center; }
  .ph span { font-size: 9pt; }
  section { page-break-inside: auto; }
  a { color: #146; text-decoration: none; }
  a[href^="#"] { color: #146; font-weight: 600; }
  .xref { color: #146; font-weight: 600; }
  .cover { text-align: center; padding-top: 28mm; page-break-after: always; }
  .cover img.logo { width: 46mm; height: auto; margin-bottom: 8px; }
  .cover .role { font-size: 18pt; color: #146; margin-top: 6px; }
  .cover .meta { color: #777; margin-top: 12px; font-size: 10pt; }
</style></head><body>
<div class="cover">
  ${existsSync(join(ROOT, 'assets', 'logo.png'))
    ? `<img class="logo" src="file://${join(ROOT, 'assets', 'logo.png')}" alt="logo">`
    : ''}
  <div class="role">${title}</div>
  <div class="meta">${subtitle}<br/>KSW HR &amp; Payroll · ${new Date().toISOString().slice(0, 10)}</div>
</div>
${body}
</body></html>`;

  const htmlPath = join(OUT, `_build-${outBase}.html`);
  const pdfPath = join(OUT, `${outBase}.pdf`);
  writeFileSync(htmlPath, html);
  execFileSync('google-chrome-stable', [
    '--headless', '--no-sandbox', '--disable-gpu', '--no-pdf-header-footer',
    '--run-all-compositor-stages-before-draw', '--virtual-time-budget=20000',
    `--print-to-pdf=${pdfPath}`, `file://${htmlPath}`,
  ], { stdio: 'pipe' });
  console.log(`  ✓ ${pdfPath}`);
  return pdfPath;
}

function gettingStarted(lang) {
  const p = join(ROOT, lang, '00-getting-started.md');
  return existsSync(p) ? [p] : [];
}

// a focused PDF per persona — ONLY that role's guides (general basics live in
// the separate General handbook)
function buildPersona(lang, persona) {
  const [slug, en, ar] = PERSONA_DOCS[persona];
  const files = personaFiles(lang, persona);
  if (files.length === 0) return;
  const title = lang === 'ar' ? ar : en;
  const sub = lang === 'ar' ? 'دليل تدريب المستخدم' : 'User Training Guide';
  renderDoc(lang, files, `KSW-${slug}-Guide-${lang.toUpperCase()}`, title, sub);
}

// the shared basics everyone reads (login, navigation, notifications)
function buildGeneral(lang) {
  const files = gettingStarted(lang);
  if (files.length === 0) return;
  const title = lang === 'ar' ? 'الدليل العام (البداية)' : 'General Guide (Getting Started)';
  const sub = lang === 'ar' ? 'للجميع — يُقرأ أولاً' : 'For everyone — read first';
  renderDoc(lang, files, `KSW-General-Guide-${lang.toUpperCase()}`, title, sub);
}

// the full comprehensive manual (optional)
function buildFull(lang) {
  const title = lang === 'ar' ? 'دليل استخدام نظام KSW' : 'KSW System — User Manual';
  const sub = lang === 'ar' ? 'الدليل الشامل لجميع الأدوار' : 'Complete manual — all roles';
  renderDoc(lang, orderedFiles(lang), `KSW-User-Manual-${lang.toUpperCase()}`, title, sub);
}

// ---- CLI ----------------------------------------------------------------
// node build_pdf.mjs            -> per-persona PDFs, both languages
// node build_pdf.mjs en         -> per-persona PDFs, English only
// node build_pdf.mjs full       -> comprehensive manual, both languages
// node build_pdf.mjs full en    -> comprehensive manual, English only
const args = process.argv.slice(2);
const wantFull = args.includes('full');
const lang = args.find(a => a === 'en' || a === 'ar');
const langs = lang ? [lang] : ['en', 'ar'];

for (const l of langs) {
  console.log(`\n[${l}]`);
  if (wantFull) { buildFull(l); continue; }
  buildGeneral(l);
  for (const persona of Object.keys(PERSONA_DOCS)) buildPersona(l, persona);
}
console.log('\nDone.');
