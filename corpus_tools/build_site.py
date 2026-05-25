"""
Build local static site from translated .md files.

Reads `Translated/{book}/{chapter_ru}/*.md` and produces
`Translated/Site/{book_slug}/{chapter_slug}/{NN}.html` plus TOC indexes
at each level.

Structure matches the planned wikibooks hierarchy:
  Зоар (Перуш ха-Сулам) → Том N → Глава → Статья (с ивр. названием)

Usage:  python corpus_tools/build_site.py
"""
from __future__ import annotations
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import parasha  # local module: Hebrew calendar + parasha cycle

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(os.environ.get("HEB_ROOT") or Path(__file__).resolve().parents[1])
TRANSLATED = ROOT / "Translated"
SITE = TRANSLATED / "Site"
CATALOG = ROOT / "Source" / "articles_catalog.json"

BOOK_SLUGS = {
    0: "vved",
    1: "bereshit",
    2: "shemot",
    3: "vayikra",
    4: "bamidbar",
    5: "devarim",
}

# Russian transliteration fallback for parashot that may be absent from
# the catalog (no Sulam article yet) but still appear in the weekly cycle.
PARASHA_RU_FALLBACK = {
    "Bereshit": "Берешит",
    "Noach": "Ноах",
    "Lech_Lecha": "Лех Леха",
    "Vayera": "Ваера",
    "Chayei_Sara": "Хайей Сара",
    "Toldot": "Толдот",
    "Vayetzei": "Ваеце",
    "Vayishlach": "Ваишлах",
    "Vayeshev": "Ваешев",
    "Miketz": "Микец",
    "Vayigash": "Ваигаш",
    "Vayechi": "Ваехи",
    "Shemot": "Шмот",
    "Vaera": "Ваэра",
    "Bo": "Бо",
    "Beshalach": "Бешалах",
    "Yitro": "Итро",
    "Mishpatim": "Мишпатим",
    "Terumah": "Трума",
    "Tetzaveh": "Тецаве",
    "Ki_Tisa": "Ки Тиса",
    "Vayakhel": "Ваякhель",
    "Pekudei": "Пекудей",
    "Vayikra": "Ваикра",
    "Tzav": "Цав",
    "Shmini": "Шмини",
    "Tazria": "Тазриа",
    "Metzora": "Мецора",
    "Achrei_Mot": "Ахарей Мот",
    "Kedoshim": "Кедошим",
    "Emor": "Эмор",
    "Behar": "Беhар",
    "Bechukotai": "Бехукотай",
    "Bamidbar": "Бамидбар",
    "Nasso": "Насо",
    "Beha'alotcha": "Беhаалотха",
    "Sh'lach": "Шлах",
    "Korach": "Корах",
    "Chukat": "Хукат",
    "Balak": "Балак",
    "Pinchas": "Пинхас",
    "Matot": "Матот",
    "Mas'ei": "Масэй",
    "Devarim": "Дварим",
    "Vaetchanan": "Ваэтханан",
    "Eikev": "Экев",
    "Re'eh": "Реэ",
    "Shoftim": "Шофтим",
    "Ki_Teitzei": "Ки Теце",
    "Ki_Tavo": "Ки Таво",
    "Nitzavim": "Ницавим",
    "Vayeilech": "Ваелех",
    "Ha'Azinu": "hаазину",
    "V'Zot_HaBerachah": "Везот hа-Бераха",
}

CSS = """
:root {
  --fg: #1a1a1a;
  --muted: #666;
  --accent: #6b2d5c;
  --bg: #fdfcf7;
  --card: #fff;
  --border: #e0dbce;
  --done: #2d7a3e;
  --pending: #999;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Georgia, 'Times New Roman', Times, serif;
  font-size: 18px;
  line-height: 1.7;
  color: var(--fg);
  background: var(--bg);
}
.wrap {
  max-width: 820px;
  margin: 0 auto;
  padding: 2.5rem 1.5rem 4rem;
}
header.breadcrumbs {
  font-size: 0.9rem;
  color: var(--muted);
  margin-bottom: 1.5rem;
  padding-bottom: 0.8rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 1rem;
}
header.breadcrumbs .crumbs { flex: 1; min-width: 0; }
header.breadcrumbs a {
  color: var(--muted);
  text-decoration: none;
}
header.breadcrumbs a:hover { color: var(--accent); text-decoration: underline; }
header.breadcrumbs .sep { margin: 0 0.4rem; }
.search-form {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-shrink: 0;
  margin: 0;
}
.search-form input[type="search"] {
  width: 180px;
  padding: 0.35rem 0.6rem;
  font-family: inherit;
  font-size: 0.9rem;
  font-weight: normal;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--card);
  color: var(--fg);
}
.search-form input[type="search"]::-webkit-search-cancel-button { display: none; }
.search-form input[type="search"]:focus {
  outline: none;
  border-color: var(--accent);
}
.search-form input[type="search"]::placeholder { color: var(--muted); font-weight: normal; }
.search-form .search-btn {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.3rem 0.5rem;
  cursor: pointer;
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.search-form .search-btn:hover { color: var(--accent); border-color: var(--accent); }
.search-form .search-btn svg { display: block; }
@media (max-width: 640px) {
  header.breadcrumbs { flex-direction: column; align-items: stretch; gap: 0.6rem; }
  .search-form input[type="search"] { width: 100%; }
}
.search-page #search-results {
  --pagefind-ui-scale: 1;
  --pagefind-ui-primary: var(--accent);
  --pagefind-ui-text: var(--fg);
  --pagefind-ui-background: var(--card);
  --pagefind-ui-border: var(--border);
  --pagefind-ui-tag: var(--bg);
  --pagefind-ui-border-width: 1px;
  --pagefind-ui-border-radius: 4px;
  --pagefind-ui-font: Georgia, 'Times New Roman', Times, serif;
}
.search-page #search-results .pagefind-ui__search-input {
  font-weight: normal;
  font-family: Georgia, 'Times New Roman', Times, serif;
  padding-right: calc(20px * var(--pagefind-ui-scale)) !important;
}
.search-page #search-results .pagefind-ui__search-clear { display: none !important; }
h1 {
  font-size: 1.9rem;
  margin: 0 0 1.5rem;
  color: var(--accent);
  font-weight: normal;
}
h2 {
  font-size: 1.4rem;
  margin: 2.2rem 0 0.8rem;
  color: var(--accent);
  font-weight: normal;
}
p { margin: 0 0 1rem; text-align: justify; }
p.para-start { padding-left: 1.5rem; }
strong { font-weight: bold; }
em { font-style: italic; }
a { color: var(--accent); }
a:hover { color: #4a1e42; }
ul.toc { list-style: none; padding: 0; margin: 0; }
ul.toc li {
  padding: 0.6rem 0.8rem;
  border-bottom: 1px dashed var(--border);
  display: flex;
  align-items: baseline;
  gap: 0.8rem;
}
ul.toc li:last-child { border-bottom: none; }
ul.toc a {
  text-decoration: none;
  flex: 1;
  color: var(--fg);
}
ul.toc a:hover { color: var(--accent); text-decoration: underline; }
ul.toc .num {
  display: inline-block;
  min-width: 2.2rem;
  color: var(--muted);
  font-size: 0.9rem;
  text-align: right;
}
ul.toc .he {
  color: var(--muted);
  font-size: 0.95rem;
  direction: rtl;
  font-family: 'David', 'Times New Roman', serif;
}
ul.toc .status {
  font-size: 0.85rem;
  color: var(--pending);
  min-width: 5rem;
  text-align: right;
}
ul.toc .status.done { color: var(--done); }
ul.toc li.pending a { color: var(--muted); pointer-events: none; }
.meta {
  color: var(--muted);
  font-size: 0.9rem;
  margin-bottom: 1.5rem;
}
article {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2rem 2.2rem;
}
article p:first-of-type { margin-top: 0; }
nav.article-nav {
  display: flex;
  justify-content: space-between;
  margin-top: 2rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  font-size: 0.95rem;
}
nav.article-nav a { text-decoration: none; }
footer.footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.85rem;
  text-align: center;
}
footer.footer p { margin: 0.35rem 0; line-height: 1.5; }
footer.footer a { color: var(--muted); text-decoration: underline; }
footer.footer a:hover { color: var(--accent); }
section.parasha-widget {
  margin-top: 2.5rem;
  padding-top: 1.5rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.85rem;
  line-height: 1.5;
}
section.parasha-widget p {
  text-align: left;
  margin: 0.35rem 0;
}
section.parasha-widget p:last-child { margin-bottom: 0; }
"""


def slugify_parasha(chapter_en: str) -> str:
    s = chapter_en.lower().replace("'", "").replace("_", "-")
    s = re.sub(r"[^a-z0-9\-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def md_to_html(md: str) -> tuple[str, str]:
    """Return (heading_text, body_html). Body is a series of <p>/<h2> blocks."""
    lines = md.splitlines()
    heading = ""
    body_lines: list[str] = []

    for line in lines:
        if not heading and line.startswith("# "):
            heading = line[2:].strip()
            continue
        body_lines.append(line)

    # Split by blank lines into blocks.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in body_lines:
        if line.strip() == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    html_parts: list[str] = []
    para_start_rx = re.compile(r"^\d+\)\s")
    for blk in blocks:
        text = "\n".join(blk).strip()
        if not text:
            continue
        if text.startswith("## "):
            html_parts.append(f"<h2>{render_inline(text[3:].strip())}</h2>")
        elif para_start_rx.match(text):
            # Первый абзац нумерованного параграфа `N)` — лёгкий отступ
            # слева, как в VS Code Preview для списков.
            html_parts.append(
                f'<p class="para-start">{render_inline(text)}</p>')
        else:
            html_parts.append(f"<p>{render_inline(text)}</p>")

    return heading, "\n".join(html_parts)


def render_inline(s: str) -> str:
    """
    Render **bold**, *italic*, ***bold-italic*** with proper nesting.

    Uses a state-machine over runs of `*`. Each `***` toggles both bold and
    italic (closes what's open, opens what's closed) — which correctly handles
    both `***word***` (bold-italic atom) and `*ит*** ...**` collisions (closing
    italic immediately followed by closing bold).
    """
    s = html.escape(s, quote=False)
    out = []
    i = 0
    italic = False
    bold = False
    while i < len(s):
        if s[i] != "*":
            out.append(s[i])
            i += 1
            continue
        # count run of stars
        j = i
        while j < len(s) and s[j] == "*":
            j += 1
        n = j - i

        # process in units of 3→2→1 to match CommonMark convention
        while n > 0:
            if n >= 3:
                # *** — toggle both
                if italic and bold:
                    out.append("</em></strong>")
                    italic = bold = False
                elif not italic and not bold:
                    out.append("<strong><em>")
                    italic = bold = True
                elif italic:
                    out.append("</em><strong>")
                    italic = False
                    bold = True
                else:   # bold
                    out.append("</strong><em>")
                    bold = False
                    italic = True
                n -= 3
            elif n == 2:
                out.append("</strong>" if bold else "<strong>")
                bold = not bold
                n -= 2
            else:  # n == 1
                out.append("</em>" if italic else "<em>")
                italic = not italic
                n -= 1
        i = j

    # Auto-close anything left open (defensive; unbalanced input should be
    # flagged separately by a lint tool, but we don't want to emit broken HTML)
    if italic:
        out.append("</em>")
    if bold:
        out.append("</strong>")
    return "".join(out)


FOOTER_HTML = (
    '<footer class="footer">\n'
    '<p class="source">Источник — «Перуш hа-Сулам» рава Йегуды Ашлага '
    '(Бааль hа-Сулам, 1885–1954). Перевод с иврита и арамейского.</p>\n'
    '<p class="attrib">Переведено Claude Opus 4.7 от Anthropic и '
    '<a href="https://proza.ru/avtor/agent017" rel="author">Элиягу Бар Малей</a>.</p>\n'
    '<p class="license">Текст доступен на условиях '
    '<a href="https://creativecommons.org/licenses/by/4.0/deed.ru" rel="license">'
    'Creative Commons Attribution 4.0 (CC BY 4.0)</a> — свободное копирование, '
    'распространение и адаптация при указании авторов и ссылки на этот сайт.</p>\n'
    '</footer>\n'
)


def build_parasha_widget_html(
    parasha_catalog: dict, all_article_urls: list[str] | None = None
) -> str:
    """Generate <section> + <script> for the weekly-parasha helper.

    Embeds two tables:
      PARASHA_INFO: parasha key → {ru, url|null}.
      PARASHA_TABLE: ISO date (Saturday) → parasha key (or "FESTIVAL").
    Client-side JS finds today's relevant Saturday, looks up the parasha,
    and renders. If the parasha has no Sulam article on the site, it walks
    the table to find the nearest available parashot before & after.

    `all_article_urls` (relative to SITE root) backs the "🪄 Мне повезет!"
    link: each click picks a fresh random article. Ctrl/Shift/middle-click
    are handled natively because we mutate `href` on `mousedown` *before*
    the browser's click dispatch reads it.
    """
    info: dict[str, dict] = {}
    for key in parasha.PARASHOT:
        if key in parasha_catalog:
            entry = parasha_catalog[key]
            # Глава всегда имеет index.html (со списком статей, в т.ч. pending),
            # поэтому ссылка валидна с момента сборки сайта — независимо от
            # того, начат ли её перевод.
            url = f'{entry["book_slug"]}/{entry["slug"]}/index.html'
            info[key] = {"ru": entry["ru"], "url": url}
        else:
            info[key] = {
                "ru": PARASHA_RU_FALLBACK.get(key, key),
                "url": None,
            }

    # Build Saturday → key table for current ±15 years.
    from datetime import date as _date
    cur_year = _date.today().year
    table = parasha.build_table(cur_year - 5, cur_year + 25)

    info_json = json.dumps(info, ensure_ascii=False)
    table_json = json.dumps(table, ensure_ascii=False)
    articles_json = json.dumps(all_article_urls or [], ensure_ascii=False)

    js = (
        "(function(){\n"
        f"const INFO = {info_json};\n"
        f"const TABLE = {table_json};\n"
        f"const ARTICLES = {articles_json};\n"
        "const MONTHS = ['января','февраля','марта','апреля','мая','июня',"
        "'июля','августа','сентября','октября','ноября','декабря'];\n"
        "function fmtDate(iso){\n"
        "  const [y,m,d] = iso.split('-').map(Number);\n"
        "  return d+' '+MONTHS[m-1]+' '+y;\n"
        "}\n"
        "function nextSaturday(d){\n"
        "  // 0=Sun..6=Sat. If today is Sat, return today.\n"
        "  const dow = d.getDay();\n"
        "  const delta = (6 - dow) % 7;\n"
        "  const r = new Date(d);\n"
        "  r.setDate(r.getDate() + delta);\n"
        "  return r;\n"
        "}\n"
        "function isoOf(d){\n"
        "  const y = d.getFullYear();\n"
        "  const m = String(d.getMonth()+1).padStart(2,'0');\n"
        "  const day = String(d.getDate()).padStart(2,'0');\n"
        "  return y+'-'+m+'-'+day;\n"
        "}\n"
        "function partsOf(key){ return key.split('+'); }\n"
        "function hasUrl(key){\n"
        "  return partsOf(key).some(p => INFO[p] && INFO[p].url);\n"
        "}\n"
        "function renderName(key){\n"
        "  const parts = partsOf(key);\n"
        "  return parts.map(p => {\n"
        "    const r = INFO[p];\n"
        "    if (!r) return p;\n"
        "    if (r.url) return '<a href=\"'+r.url+'\">'+r.ru+'</a>';\n"
        "    return r.ru;\n"
        "  }).join(' + ');\n"
        "}\n"
        "function walkFor(iso, dir){\n"
        "  // Walk Saturdays in `dir` (±1 weeks) until we find an available parasha.\n"
        "  const [y,m,d] = iso.split('-').map(Number);\n"
        "  let cur = new Date(y, m-1, d);\n"
        "  for (let i = 0; i < 60; i++){\n"
        "    cur.setDate(cur.getDate() + dir*7);\n"
        "    const k = TABLE[isoOf(cur)];\n"
        "    if (!k || k === 'FESTIVAL') continue;\n"
        "    if (hasUrl(k)) return {iso: isoOf(cur), key: k};\n"
        "  }\n"
        "  return null;\n"
        "}\n"
        "const today = new Date();\n"
        "const sat = nextSaturday(today);\n"
        "const satIso = isoOf(sat);\n"
        "const key = TABLE[satIso];\n"
        "const root = document.getElementById('parasha-widget');\n"
        "if (!root) return;\n"
        "const POYASN = '<p><a href=\"poyasneniya/index.html\">'\n"
        "             + 'Необходимые пояснения (Элиягу Бар Малей)</a></p>'\n"
        "             + (ARTICLES.length\n"
        "                ? '<p><a id=\"lucky-link\" href=\"\">🪄 Мне повезет!</a></p>'\n"
        "                : '');\n"
        "let html = '';\n"
        "if (!key){\n"
        "  html = '<p>Недельная глава Торы — дата за пределами таблицы.</p>'\n"
        "       + POYASN;\n"
        "} else if (key === 'FESTIVAL'){\n"
        "  const prev = walkFor(satIso, -1);\n"
        "  const next = walkFor(satIso, +1);\n"
        "  html = '<p>Шаббат '+fmtDate(satIso)+' — праздничный, недельная глава не читается.</p>'\n"
        "       + '<p>Ближайшие доступные: '\n"
        "       + (prev ? renderName(prev.key)+' ('+fmtDate(prev.iso)+')' : 'нет ранее')\n"
        "       + ', либо '\n"
        "       + (next ? renderName(next.key)+' ('+fmtDate(next.iso)+')' : 'нет позже')\n"
        "       + '.</p>'\n"
        "       + POYASN;\n"
        "} else if (hasUrl(key)){\n"
        "  html = '<p>Сегодня — '+renderName(key)+' (Шаббат '+fmtDate(satIso)+').</p>'\n"
        "       + POYASN;\n"
        "} else {\n"
        "  const prev = walkFor(satIso, -1);\n"
        "  const next = walkFor(satIso, +1);\n"
        "  html = '<p>Сегодня — '+renderName(key)+' (Шаббат '+fmtDate(satIso)+').</p>'\n"
        "       + '<p>Ближайшие доступные: '\n"
        "       + (prev ? renderName(prev.key)+' ('+fmtDate(prev.iso)+')' : 'нет ранее')\n"
        "       + ', либо '\n"
        "       + (next ? renderName(next.key)+' ('+fmtDate(next.iso)+')' : 'нет позже')\n"
        "       + '.</p>'\n"
        "       + POYASN;\n"
        "}\n"
        "root.innerHTML = html;\n"
        "const lucky = document.getElementById('lucky-link');\n"
        "if (lucky && ARTICLES.length){\n"
        "  const pick = () => ARTICLES[Math.floor(Math.random()*ARTICLES.length)];\n"
        "  // Refresh href right before the browser reads it for navigation,\n"
        "  // so Ctrl/Shift/middle-click open a fresh random article in a new\n"
        "  // tab/window via native handling, and plain click does the same\n"
        "  // in the current tab. Both mouse & keyboard activation covered.\n"
        "  lucky.href = pick();\n"
        "  lucky.addEventListener('mousedown', () => { lucky.href = pick(); });\n"
        "  lucky.addEventListener('keydown', (e) => {\n"
        "    if (e.key === 'Enter' || e.key === ' ') lucky.href = pick();\n"
        "  });\n"
        "}\n"
        "})();\n"
    )

    return (
        '<section class="parasha-widget" id="parasha-widget">'
        '<p>Недельная глава Торы…</p>'
        '</section>\n'
        f'<script>{js}</script>\n'
    )


_CSS_VERSION_CACHE = {"v": ""}


def _css_version() -> str:
    if not _CSS_VERSION_CACHE["v"]:
        import hashlib
        _CSS_VERSION_CACHE["v"] = hashlib.md5(CSS.encode("utf-8")).hexdigest()[:8]
    return _CSS_VERSION_CACHE["v"]


SEARCH_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="11" cy="11" r="7"></circle>'
    '<line x1="21" y1="21" x2="16.65" y2="16.65"></line>'
    '</svg>'
)

PAGEFIND_TRANSLATIONS_JS = (
    '{\n'
    '      placeholder: "Поиск по сайту",\n'
    '      clear_search: "×",\n'
    '      load_more: "Показать ещё",\n'
    '      search_label: "Поиск",\n'
    '      filters_label: "Фильтры",\n'
    '      zero_results: "Ничего не найдено по запросу [SEARCH_TERM]",\n'
    '      many_results: "Найдено [COUNT] совпадений по [SEARCH_TERM]",\n'
    '      one_result: "Найдено [COUNT] совпадение по [SEARCH_TERM]",\n'
    '      alt_search: "Ничего по [SEARCH_TERM]. Возможно, имели в виду [DIFFERENT_TERM]",\n'
    '      search_suggestion: "Ничего по [SEARCH_TERM]. Попробуйте [DIFFERENT_TERM]",\n'
    '      searching: "Поиск по [SEARCH_TERM]…"\n'
    '    }'
)


def page(
    title: str,
    breadcrumbs: list[tuple[str, str]],
    body: str,
    depth: int,
    *,
    is_search_page: bool = False,
) -> str:
    css_href = "../" * depth + f"style.css?v={_css_version()}"
    pf_base = ("../" * depth or "./") + "pagefind/"
    search_action = ("../" * depth or "./") + "search.html"
    # Search-page reuses the same query param via its own form, so submitting
    # again should replace the current tab; everywhere else we open results
    # in a new tab.
    form_target = "" if is_search_page else ' target="_blank"'
    body_class = ' class="search-page"' if is_search_page else ""

    crumb_html_parts = []
    for i, (label, href) in enumerate(breadcrumbs):
        if i > 0:
            crumb_html_parts.append('<span class="sep">›</span>')
        if href:
            crumb_html_parts.append(f'<a href="{href}">{html.escape(label)}</a>')
        else:
            crumb_html_parts.append(html.escape(label))
    crumb_html = "".join(crumb_html_parts)

    head_extra = ""
    body_extra = ""
    if is_search_page:
        head_extra = f'<link rel="stylesheet" href="{pf_base}pagefind-ui.css">\n'
        body_extra = (
            f'<script src="{pf_base}pagefind-ui.js"></script>\n'
            '<script>\n'
            'window.addEventListener("DOMContentLoaded",function(){\n'
            '  const params = new URLSearchParams(location.search);\n'
            '  const q = params.get("q") || "";\n'
            '  const ui = new PagefindUI({\n'
            '    element: "#search-results",\n'
            '    showImages: false,\n'
            '    showSubResults: true,\n'
            '    excerptLength: 30,\n'
            '    resetStyles: false,\n'
            '    autofocus: true,\n'
            f'    translations: {PAGEFIND_TRANSLATIONS_JS}\n'
            '  });\n'
            '  if (q) { ui.triggerSearch(q); }\n'
            '});\n'
            '</script>\n'
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f'<link rel="stylesheet" href="{css_href}">\n'
        f"{head_extra}"
        f"</head>\n<body{body_class}>\n"
        '<div class="wrap">\n'
        '<header class="breadcrumbs">'
        f'<div class="crumbs">{crumb_html}</div>'
        f'<form class="search-form" action="{search_action}" method="get"{form_target} role="search">'
        '<input type="search" name="q" placeholder="Поиск" aria-label="Поиск" autocomplete="off">'
        f'<button type="submit" class="search-btn" aria-label="Найти">{SEARCH_ICON_SVG}</button>'
        '</form>'
        '</header>\n'
        f"{body}\n"
        f"{FOOTER_HTML}"
        "</div>\n"
        f"{body_extra}"
        "</body>\n</html>\n"
    )


def read_catalog() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def build(skip_pagefind: bool = False):
    catalog = read_catalog()

    # Group by book then chapter.
    by_book: dict[int, dict[str, dict]] = {}
    for art in catalog:
        bi = art["book_index"]
        pa = art["chapter"]
        by_book.setdefault(bi, {})
        if pa not in by_book[bi]:
            by_book[bi][pa] = {
                "book": art["book"],
                "book_index": bi,
                "chapter": pa,
                "chapter_ru": art["chapter_ru"],
                "chapter_order": art["chapter_order"],
                "articles": [],
            }
        by_book[bi][pa]["articles"].append(art)

    # Sort chapters by chapter_order within each book.
    for bi in by_book:
        for pa in by_book[bi]:
            by_book[bi][pa]["articles"].sort(key=lambda a: a["article_index"])

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "style.css").write_text(CSS, encoding="utf-8")

    total_articles_done = 0
    total_articles_all = 0
    done_parashot = 0
    total_parashot = 0

    book_entries_for_index = []
    # Maps catalog `chapter` key → {ru, book_slug, slug, done, total} —
    # used by the parasha widget to decide if/how to link a parasha.
    parasha_catalog: dict[str, dict] = {}
    # Flat list of relative URLs for every completed article — feeds the
    # "🪄 Мне повезет!" link on the main index.
    all_article_urls: list[str] = []

    for bi in sorted(by_book.keys()):
        book_slug = BOOK_SLUGS[bi]
        book_dir_site = SITE / book_slug
        book_dir_site.mkdir(exist_ok=True)

        chapters = sorted(by_book[bi].values(), key=lambda p: p["chapter_order"])
        book_full_name = chapters[0]["book"]

        book_parashot_done = 0
        book_parashot_total = len(chapters)
        book_articles_done = 0
        book_articles_total = 0
        chapter_entries = []

        for p in chapters:
            pa_slug = slugify_parasha(p["chapter"])
            pa_dir_src = TRANSLATED / p["book"] / p["chapter_ru"]
            pa_dir_site = book_dir_site / pa_slug
            pa_dir_site.mkdir(exist_ok=True)

            articles = p["articles"]
            art_total = len(articles)
            art_done = 0

            article_entries = []
            for idx, art in enumerate(articles):
                nn = art["article_index"]
                md_path = pa_dir_src / f"{nn:03d}.md"
                prev_art = articles[idx - 1] if idx > 0 else None
                next_art = articles[idx + 1] if idx < len(articles) - 1 else None

                if md_path.exists():
                    md = md_path.read_text(encoding="utf-8")
                    heading, body = md_to_html(md)
                    art_done += 1

                    nav_prev = ""
                    nav_next = ""
                    if prev_art:
                        nav_prev = (
                            f'<a href="{prev_art["article_index"]:03d}.html">'
                            f"← Статья {prev_art['article_index']}</a>"
                        )
                    if next_art:
                        nav_next = (
                            f'<a href="{next_art["article_index"]:03d}.html">'
                            f"Статья {next_art['article_index']} →</a>"
                        )

                    article_body = (
                        f'<h1 data-pagefind-meta="title">{html.escape(heading)}</h1>\n'
                        f'<p class="meta" data-pagefind-meta="chapter">'
                        f"{html.escape(p['chapter_ru'])} · Сулам §{art['sulam_paragraphs'][0]}"
                        f"–{art['sulam_paragraphs'][1]} · "
                        f'<span class="he" dir="rtl">{html.escape(art["he_title"])}</span>'
                        f"</p>\n"
                        f"<article data-pagefind-body>\n{body}\n</article>\n"
                        f'<nav class="article-nav">'
                        f'<span>{nav_prev or "&nbsp;"}</span>'
                        f'<a href="index.html">Оглавление главы</a>'
                        f'<span>{nav_next or "&nbsp;"}</span>'
                        f"</nav>\n"
                    )
                    bc = [
                        ("Главная", "../../index.html"),
                        (book_full_name, "../index.html"),
                        (p["chapter_ru"], "index.html"),
                        (f"Статья {nn}", ""),
                    ]
                    (pa_dir_site / f"{nn:03d}.html").write_text(
                        page(heading, bc, article_body, depth=2),
                        encoding="utf-8",
                    )
                    article_entries.append(
                        {"nn": nn, "title": heading, "he": art["he_title"], "done": True}
                    )
                    all_article_urls.append(
                        f"{book_slug}/{pa_slug}/{nn:03d}.html"
                    )
                else:
                    article_entries.append(
                        {
                            "nn": nn,
                            "title": f"Статья {nn}",
                            "he": art["he_title"],
                            "done": False,
                        }
                    )

            # Chapter index
            status_str = f"{art_done}/{art_total}"
            is_done = art_done == art_total
            if is_done:
                book_parashot_done += 1

            li_items = []
            for ae in article_entries:
                status_cls = "status done" if ae["done"] else "status"
                status_text = "✅" if ae["done"] else "⏸"
                if ae["done"]:
                    link = f'<a href="{ae["nn"]:03d}.html">{html.escape(ae["title"])}</a>'
                    li_cls = ""
                else:
                    link = f'<a>{html.escape(ae["title"])}</a>'
                    li_cls = ' class="pending"'
                li_items.append(
                    f'<li{li_cls}>'
                    f'<span class="num">{ae["nn"]}.</span>'
                    f'{link}'
                    f'<span class="he" dir="rtl">{html.escape(ae["he"])}</span>'
                    f'<span class="{status_cls}">{status_text}</span>'
                    f"</li>"
                )

            chapter_body = (
                f"<h1>{html.escape(p['chapter_ru'])}</h1>\n"
                f'<p class="meta">Книга: {html.escape(book_full_name)} · '
                f"Прогресс: {status_str} статей"
                f"{' · ✅ Завершена' if is_done else ''}</p>\n"
                f'<ul class="toc">\n{chr(10).join(li_items)}\n</ul>\n'
            )
            bc = [
                ("Главная", "../../index.html"),
                (book_full_name, "../index.html"),
                (p["chapter_ru"], ""),
            ]
            (pa_dir_site / "index.html").write_text(
                page(p["chapter_ru"], bc, chapter_body, depth=2),
                encoding="utf-8",
            )

            chapter_entries.append(
                {
                    "slug": pa_slug,
                    "name": p["chapter_ru"],
                    "en": p["chapter"],
                    "done": art_done,
                    "total": art_total,
                    "is_done": is_done,
                }
            )
            parasha_catalog[p["chapter"]] = {
                "ru": p["chapter_ru"],
                "book_slug": book_slug,
                "slug": pa_slug,
                "done": art_done,
                "total": art_total,
            }
            book_articles_done += art_done
            book_articles_total += art_total

        # Book index
        li_items = []
        for pe in chapter_entries:
            status_cls = "status done" if pe["is_done"] else "status"
            status_text = (
                "✅ завершено" if pe["is_done"] else f'{pe["done"]}/{pe["total"]}'
            )
            if pe["done"] > 0:
                link = (
                    f'<a href="{pe["slug"]}/index.html">{html.escape(pe["name"])}</a>'
                )
                li_cls = ""
            else:
                link = f'<a>{html.escape(pe["name"])}</a>'
                li_cls = ' class="pending"'
            li_items.append(
                f'<li{li_cls}>'
                f'{link}'
                f'<span class="{status_cls}">{status_text}</span>'
                f"</li>"
            )

        book_body = (
            f"<h1>{html.escape(book_full_name)}</h1>\n"
            f'<p class="meta">Главы: {book_parashot_done}/{book_parashot_total} · '
            f"Статей: {book_articles_done}/{book_articles_total}</p>\n"
            f'<ul class="toc">\n{chr(10).join(li_items)}\n</ul>\n'
        )
        bc = [("Главная", "../index.html"), (book_full_name, "")]
        (book_dir_site / "index.html").write_text(
            page(book_full_name, bc, book_body, depth=1), encoding="utf-8"
        )

        book_entries_for_index.append(
            {
                "slug": book_slug,
                "name": book_full_name,
                "parashot_done": book_parashot_done,
                "parashot_total": book_parashot_total,
                "articles_done": book_articles_done,
                "articles_total": book_articles_total,
            }
        )
        total_articles_done += book_articles_done
        total_articles_all += book_articles_total
        done_parashot += book_parashot_done
        total_parashot += book_parashot_total

    # Main index
    li_items = []
    for be in book_entries_for_index:
        status_cls = (
            "status done"
            if be["parashot_done"] == be["parashot_total"]
            else "status"
        )
        status_text = f'{be["parashot_done"]}/{be["parashot_total"]} главы'
        if be["articles_done"] > 0:
            link = f'<a href="{be["slug"]}/index.html">{html.escape(be["name"])}</a>'
            li_cls = ""
        else:
            link = f'<a>{html.escape(be["name"])}</a>'
            li_cls = ' class="pending"'
        li_items.append(
            f'<li{li_cls}>'
            f'{link}'
            f'<span class="{status_cls}">{status_text}</span>'
            f"</li>"
        )

    parasha_widget = build_parasha_widget_html(parasha_catalog, all_article_urls)
    main_body = (
        "<h1>Книга Зоар (Перуш ха-Сулам)</h1>\n"
        '<p class="meta">Русский перевод. '
        f"Прогресс: {done_parashot}/{total_parashot} главы · "
        f"{total_articles_done}/{total_articles_all} статей.</p>\n"
        f'<ul class="toc">\n{chr(10).join(li_items)}\n</ul>\n'
        f"{parasha_widget}"
    )
    bc = [("Главная", "")]
    (SITE / "index.html").write_text(
        page("Книга Зоар (Перуш ха-Сулам)", bc, main_body, depth=0),
        encoding="utf-8",
    )

    # Search results page (loaded by the breadcrumb form via ?q=).
    search_body = (
        "<h1>Поиск</h1>\n"
        '<div id="search-results"></div>\n'
    )
    search_bc = [("Главная", "index.html"), ("Поиск", "")]
    (SITE / "search.html").write_text(
        page("Поиск — Книга Зоар", search_bc, search_body,
             depth=0, is_search_page=True),
        encoding="utf-8",
    )

    print(
        f"Site built: {SITE}\n"
        f"  Books: {len(book_entries_for_index)}\n"
        f"  Chapters done: {done_parashot}/{total_parashot}\n"
        f"  Articles done: {total_articles_done}/{total_articles_all}"
    )

    # Pagefind re-indexing is slow (minutes once corpus grows); skip it
    # when the caller explicitly asks (`--no-pagefind` / env). The
    # previously-built index stays on disk — search will return slightly
    # stale results until the next full build (chapter close).
    if skip_pagefind:
        print("Pagefind: skipped (--no-pagefind)")
    else:
        pf_summary = build_pagefind_index()
        if pf_summary:
            print(pf_summary)


def build_pagefind_index() -> str:
    """Run the pagefind CLI to (re)build the search index in `Site/pagefind/`.

    The index is part of the site (loaded by `search.html`), so it must stay
    in sync with the generated HTML. Failures are non-fatal: if the CLI is
    missing or returns an error, we print a warning and let `build()` finish
    — the existing index (if any) remains untouched.

    Returns a one-line summary string for callers to print/log, or "" on
    skip/failure.
    """
    pf_bin = shutil.which("pagefind") or shutil.which("pagefind.cmd")
    if not pf_bin:
        print(
            "Pagefind: CLI not found on PATH; skipping search-index build.\n"
            "  Install with: npm install -g pagefind",
            file=sys.stderr,
        )
        return ""

    try:
        proc = subprocess.run(
            [pf_bin, "--site", str(SITE), "--output-subdir", "pagefind"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
    except Exception as e:
        print(f"Pagefind: invocation failed: {e}", file=sys.stderr)
        return ""

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        print(f"Pagefind: rc={proc.returncode}\n{tail}", file=sys.stderr)
        return ""

    summary_lines = [
        ln.strip() for ln in (proc.stdout or "").splitlines()
        if ln.strip().startswith("Indexed ") or ln.strip().startswith("Finished")
    ]
    return "Pagefind: " + "; ".join(summary_lines) if summary_lines else "Pagefind: index built"


if __name__ == "__main__":
    # CLI: --no-pagefind skips the slow search-index rebuild.
    # Env BUILD_SITE_NO_PAGEFIND=1 has the same effect.
    skip_pf = ("--no-pagefind" in sys.argv[1:]) or bool(
        os.environ.get("BUILD_SITE_NO_PAGEFIND")
    )
    build(skip_pagefind=skip_pf)
