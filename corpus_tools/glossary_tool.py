#!/usr/bin/env python3
"""
glossary_tool.py — CLI для словаря проекта Зоар.
Словарь большой (>300 КБ / ~70k токенов); все операции — через этот скрипт,
чтобы не загружать JSON в контекст LLM.

Команды:
    lookup   --paragraphs F --range S E [-c] [--raw]   Правила для иврит-текста
    search   --he T | --ru T | --cat C | --id ID       Поиск правил
    add      --he ... --ru ... --cat ... [...]         Добавить одно правило
    update   --id ID [--ru RU] [...]                   Изменить одно правило
    delete   --id ID                                   Удалить одно правило
    batch-add    --file F                              Пакетно добавить (было batch)
    batch-update --file F                              Атомарно выполнить массу операций
    stats                                              Сводка
    conflicts                                          Найти конфликты (6 проверок)
    validate                                           Проверить схему/целостность
    diff     --from V --to V                           Сравнить версии
    dump     --ids A,B,C                               Полный JSON правил
    version                                            Текущая версия
    set-meta --articles-processed N | --note-key K --note-value V

Флаг --glossary PATH переопределяет авто-выбор.

Особенности lookup (по умолчанию):
  * Матчинг `he` — по границам слова (не-ивритские символы слева/справа),
    с опциональными ивр. префиксами (ה/ב/ל/ש/ו/מ/כ) для одиночных слов.
    Фразы (с пробелом) и аббревиатуры (с кавычкой) — строгий матч без префиксов.
  * Дедупликация: если один и тот же вариант `he` совпал в двух
    терминологических правилах, оставляется с высшим приоритетом (priority=1
    сильнее priority=2).
  * Conflicts_with: если правило A матчится и упоминает правило B в
    conflicts_with, и у A приоритет выше, B подавляется.
  * Семантические/синтаксические/стилистические правила не фильтруются —
    это инструкции, применяются всегда.

Чтобы отключить фильтрацию (для отладки/ревизий): `lookup ... --raw`.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from datetime import date
from copy import deepcopy

# Force UTF-8 on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


# ── Константы ────────────────────────────────────────────────────────

HEB_LETTERS = "\u05D0-\u05EA"
HEB_PREFIXES = "\u05D4\u05D1\u05DC\u05E9\u05D5\u05DE\u05DB"  # ה ב ל ש ו מ כ

# Допустимые ивр./арам. словоизменительные суффиксы на правой границе
# одиночных терминологических термов. Цель — ловить, например, `דין` (din) в
# формах `דינים` (мн. ч. иврит), `דינין` (мн. ч. арам.), `דינא` (арам.
# эмф.), `דיניא`/`דינייא` (арам. мн. эмф.). Порядок — от длинного к
# короткому, чтобы regex предпочитал самый длинный суффикс. Применяется
# ТОЛЬКО к одиночным словам (без пробелов и кавычек) длиной >= 3 ивр.
# букв, иначе риск ложных срабатываний слишком велик (см. make_he_regex).
HEB_SUFFIXES = [
    "\u05D9\u05D9\u05D0",  # ייא  — Aram. emphatic plural with yod
    "\u05D9\u05D0",         # יא   — Aram. emphatic plural
    "\u05D9\u05DD",         # ים   — Heb. masc. plural
    "\u05D9\u05DF",         # ין   — Aram. masc. plural
    "\u05D5\u05EA",         # ות   — Heb. fem. plural
    "\u05D0",                # א    — Aram. emphatic (determinate)
]
HEB_SUFFIX_GROUP = "(?:" + "|".join(HEB_SUFFIXES) + ")?"

# Ивритские конечные формы букв. При наслоении суффикса конечная буква
# в основе теряет 'финальность' (`דין` → `דינים`: финальный нун ן ↔ נ).
HE_FINAL_TO_REG = {
    "ך": "כ",  # ך → כ
    "ם": "מ",  # ם → מ
    "ן": "נ",  # ן → נ
    "ף": "פ",  # ף → פ
    "ץ": "צ",  # ץ → צ
}


# Унификация ивр.-пунктуации к ASCII для целей word-boundary матчинга.
# В словаре аббревиатуры пишутся через ASCII " (U+0022) и ' (U+0027), но в
# исходниках Sulam_on_Zohar ~13 глав (Achrei_Mot, Bechukotai, Behar, Emor,
# Idra_Rabba, Kedoshim, Metzora, Nasso, Sh'lach, Shmini, Tazria, Tzav,
# Vayikra) используют ивр. эквиваленты — гершаим (U+05F4) и гереш (U+05F3).
# Без нормализации правила вроде T001 (he=`הקב"ה`) не находятся в источнике
# `הקב״ה`. См. также `meta.notation_note` в glossary_*.json.
HE_PUNCT_NORMALIZE = str.maketrans({
    "״": '"',   # gershayim → ASCII double quote
    "׳": "'",   # geresh    → ASCII apostrophe
})


def normalize_he_punct(s: str) -> str:
    """ASCII-нормализация ивр. кавычек в строке (для регексов и текста)."""
    return s.translate(HE_PUNCT_NORMALIZE)


CAT_PREFIX = {
    "terminological": "T",
    "semantic": "S",
    "syntactic": "X",
    "stylistic": "Y",
}
VALID_CATEGORIES = set(CAT_PREFIX.keys())
VALID_PRIORITIES = set(range(1, 6))
REQUIRED_RULE_FIELDS = ("id", "he", "ru", "category", "priority")
ID_REF_RE = re.compile(r"\b([TSXY]\d{3})\b")


# ── Вспомогательные ──────────────────────────────────────────────────


def find_latest_glossary(base_dir: Path) -> Path:
    single = base_dir / "glossary.json"
    if single.is_file():
        return single
    cands = sorted(base_dir.glob("glossary_*.json"), reverse=True)
    if cands:
        return cands[0]
    print("ERROR: No glossary.json or glossary_*.json found in", base_dir, file=sys.stderr)
    sys.exit(1)


def load_glossary(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_glossary(data: dict, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved: {path} ({len(data['rules'])} rules)")


def next_version_path(current: Path) -> Path:
    if current.name == "glossary.json":
        return current
    m = re.search(r"(\d+)", current.stem)
    if not m:
        print("ERROR: cannot parse version from", current.name, file=sys.stderr)
        sys.exit(1)
    nnn = int(m.group(1)) + 1
    return current.parent / f"glossary_{nnn:03d}.json"


def bump_meta(data: dict):
    """Increment meta.version, update date, refresh rules_count. Mutates in place."""
    cur_ver = int(data["meta"]["version"])
    data["meta"]["version"] = f"{cur_ver + 1:03d}"
    data["meta"]["updated"] = str(date.today())
    data["meta"]["rules_count"] = len(data["rules"])


def next_id(rules: list, prefix: str) -> str:
    existing = [int(r["id"][1:]) for r in rules if r["id"].startswith(prefix) and r["id"][1:].isdigit()]
    n = max(existing, default=0) + 1
    return f"{prefix}{n:03d}"


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def split_variants(he_field: str) -> list:
    """Разбить поле `he` по ' / '."""
    return [v.strip() for v in (he_field or "").split("/") if v.strip()]


# ── Word-boundary матчинг ────────────────────────────────────────────


def make_he_regex(variant: str, allow_suffix: bool = True) -> "re.Pattern":
    """
    Скомпилировать regex для ивр. термина/фразы с границами слова.

    * Слева разрешены 0+ ивр. префиксов (ה/ב/ל/ש/ו/מ/כ) -- покрывает как
      одиночные префиксы (`בהקב"ה`, `מא"ס ב"ה`), так и комбинации
      (`שב`, `שה`, `וב`, `ומה`, `כש`, `וכש`, `וכשה` и т.д.).  Это работает
      и для одиночных слов, и для аббревиатур (с `"`/`'`), и для фраз
      (префикс прицепляется только к первому слову фразы).
    * Границы слова: не-ивр. символ (вне 05D0-05EA) или начало/конец строки.
    * Перед компиляцией `variant` проходит через `normalize_he_punct` --
      ивр. кавычки гершаим/гереш приводятся к ASCII " и '.  `cmd_lookup`
      нормализует и текст-источник, чтобы матчинг был симметричным.

    Раньше strict-ветка для variants с " / ' / пробелом не имела
    `[HEB_PREFIXES]*` слева, из-за чего, например, `בא"ס ב"ה` (с префиксом
    ב) не находилось правилом T401 (variant `א"ס ב"ה`).  По всему корпусу
    Sulam_on_Zohar таких пропусков было ~10 000 на 56 ASCII-кавыченных
    правилах словаря (баг №2 в problem_070526.md).

    Суффиксный матчинг (allow_suffix=True, по умолчанию):
      Для одиночных не-аббревиатурных слов длиной >= 3 ивр. букв на правой
      границе допустимы плюральные/эмфатические суффиксы из HEB_SUFFIXES:
      `ים` / `ין` / `ות` / `א` / `יא` / `ייא`. Если основа кончается на
      финальную форму буквы (ך/ם/ן/ף/ץ) и присутствует суффикс — финальная
      форма меняется на обычную (см. HE_FINAL_TO_REG). Так T005 (`דין`)
      ловит формы `דינים` / `דינין` / `דינא` / `דיניא` / `דינייא`. Опция
      отключается флагом `--no-suffix` в lookup для отладки/ревизий.
    """
    variant = normalize_he_punct(variant)
    bl = f"(?<![{HEB_LETTERS}])"
    br = f"(?![{HEB_LETTERS}])"
    pref = f"[{HEB_PREFIXES}]*"
    eligible_for_suffix = (
        allow_suffix
        and " " not in variant
        and '"' not in variant
        and "'" not in variant
        and len(variant) >= 3
    )
    if eligible_for_suffix and variant and variant[-1] in HE_FINAL_TO_REG:
        # Основа кончается на финальную букву. Две альтернативы:
        #  (а) точный variant без суффикса (формы изолированного слова),
        #  (б) основа с расфинализованной буквой + обязательный суффикс.
        unfinal = variant[:-1] + HE_FINAL_TO_REG[variant[-1]]
        suffix_alt = "(?:" + "|".join(HEB_SUFFIXES) + ")"
        body = (
            "(?:" + re.escape(variant)
            + "|" + re.escape(unfinal) + suffix_alt + ")"
        )
    elif eligible_for_suffix:
        # Основа кончается на обычную букву — суффикс просто опциональный.
        body = re.escape(variant) + HEB_SUFFIX_GROUP
    else:
        body = re.escape(variant)
    return re.compile(bl + pref + body + br)


def rule_hits(rule: dict, text: str, raw: bool = False, allow_suffix: bool = True) -> list:
    """
    Для terminological-правила вернуть список совпавших вариантов.
    Для остальных — всегда [] (они включаются безусловно отдельной логикой).

    `raw=True` → substring-match (старое поведение).
    """
    if rule.get("category") != "terminological":
        return []
    he = rule.get("he", "") or ""
    variants = split_variants(he)
    hits = []
    for v in variants:
        if raw:
            if v in text:
                hits.append(v)
        else:
            try:
                if make_he_regex(v, allow_suffix=allow_suffix).search(text):
                    hits.append(v)
            except re.error:
                if v in text:
                    hits.append(v)
    return hits


# ── Разрешение конфликтов в lookup ───────────────────────────────────


def resolve_conflicts(term_matches: list, emit_warnings=True) -> list:
    """
    Вход: список (rule, hits) для terminological-правил.
    Выход: отфильтрованный список правил.

    Правила подавления:
    1. Группируем по совпавшему варианту (каждый элемент списка `hits`).
       Если на одном варианте совпало несколько правил — оставляем с наивысшим
       приоритетом (min value). При равных приоритетах оставляем все (warning).
    2. `conflicts_with`: если и A, и B в kept, и B указан в A.conflicts_with,
       и priority(A) < priority(B) — подавляем B (и симметрично).
    """
    # --- Check 1: group by variant ---
    variant_bucket = {}  # variant -> list of rules
    for r, hits in term_matches:
        for v in hits:
            variant_bucket.setdefault(v, []).append(r)

    kept_ids = set()
    for v, grp in variant_bucket.items():
        grp_sorted = sorted(grp, key=lambda r: r.get("priority", 5))
        best_prio = grp_sorted[0].get("priority", 5)
        for r in grp_sorted:
            if r.get("priority", 5) == best_prio:
                kept_ids.add(r["id"])
        if emit_warnings and sum(1 for r in grp_sorted if r.get("priority", 5) == best_prio) > 1 and len(grp_sorted) > 1:
            ids = [r["id"] for r in grp_sorted if r.get("priority", 5) == best_prio]
            print(
                f"[lookup] WARN: variant '{v}' matched by multiple rules with equal priority {best_prio}: {ids}",
                file=sys.stderr,
            )

    kept = [r for r, _ in term_matches if r["id"] in kept_ids]
    # --- Check 2: conflicts_with suppression ---
    kept_by_id = {r["id"]: r for r in kept}
    suppressed = set()
    for r in kept:
        if r["id"] in suppressed:
            continue
        for cid in r.get("conflicts_with") or []:
            if cid in kept_by_id and cid not in suppressed:
                a_prio = r.get("priority", 5)
                b_prio = kept_by_id[cid].get("priority", 5)
                if a_prio < b_prio:
                    suppressed.add(cid)
                elif b_prio < a_prio:
                    suppressed.add(r["id"])
                    break
    kept = [r for r in kept if r["id"] not in suppressed]
    return kept


# ── Команды ──────────────────────────────────────────────────────────


def cmd_lookup(args, gdata, gpath):
    """Найти правила, релевантные для заданного ивр. текста."""
    if args.text:
        text = Path(args.text).read_text(encoding="utf-8")
    elif args.stdin:
        text = sys.stdin.read()
    elif args.paragraphs:
        src = json.loads(Path(args.paragraphs).read_text(encoding="utf-8"))
        he = src["he"]
        start, end = args.range
        parts = []
        for i in range(start - 1, end):
            if i < len(he):
                p = he[i]
                if isinstance(p, list):
                    parts.extend(p)
                else:
                    parts.append(str(p))
        text = " ".join(parts)
    else:
        print("ERROR: provide --text FILE, --stdin, or --paragraphs FILE --range S E", file=sys.stderr)
        sys.exit(1)

    # Нормализуем ивр. кавычки (״ → ", ׳ → ') симметрично с make_he_regex.
    # См. подробности в docstring `normalize_he_punct`.
    text_clean = normalize_he_punct(strip_html(text))

    # Collect raw matches
    term_matches = []
    other_rules = []
    for r in gdata["rules"]:
        if r["category"] == "terminological":
            hits = rule_hits(r, text_clean, raw=args.raw, allow_suffix=not getattr(args, 'no_suffix', False))
            if hits:
                term_matches.append((r, hits))
        else:
            other_rules.append(r)

    if args.raw:
        matches = [r for r, _ in term_matches] + other_rules
    else:
        matches = resolve_conflicts(term_matches) + other_rules

    matches.sort(key=lambda r: (0 if r["category"] == "terminological" else 1, r.get("priority", 5)))

    if args.compact:
        for r in matches:
            cat_tag = CAT_PREFIX.get(r["category"], "?")
            line = f'{r["id"]} [{cat_tag}] {r["he"]} → {r["ru"]}'
            if r.get("priority"):
                line += f'  (p{r["priority"]})'
            print(line)
    else:
        print(json.dumps(matches, ensure_ascii=False, indent=2))

    raw_term_n = sum(1 for _ in term_matches)
    kept_term_n = sum(1 for r in matches if r["category"] == "terminological")
    mode = "raw" if args.raw else "resolved"
    print(
        f"\n--- {len(matches)} / {len(gdata['rules'])} rules matched "
        f"({kept_term_n}/{raw_term_n} term. after {mode}) ---",
        file=sys.stderr,
    )


def cmd_search(args, gdata, gpath):
    results = gdata["rules"]
    if args.id:
        results = [r for r in results if r["id"] == args.id.upper()]
    if args.he:
        results = [r for r in results if args.he in (r.get("he") or "")]
    if args.ru:
        results = [r for r in results if args.ru.lower() in (r.get("ru") or "").lower()]
    if args.cat:
        results = [r for r in results if r.get("category", "").startswith(args.cat)]
    if args.subcat:
        results = [r for r in results if r.get("subcategory", "") == args.subcat]

    if args.compact:
        for r in results:
            print(f'{r["id"]} {r["he"]} → {r["ru"]}')
    else:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n--- {len(results)} results ---", file=sys.stderr)


def _build_rule(item: dict, rules: list, version: str, force: bool = False):
    """Построить новое правило. Бросает ValueError при конфликте (кроме force)."""
    cat = item["cat"] if "cat" in item else item.get("category")
    if cat not in CAT_PREFIX:
        raise ValueError(f"category must be one of {list(CAT_PREFIX)}; got {cat!r}")

    he = item["he"]
    ru = item["ru"]
    # Exact duplicate?
    for r in rules:
        if r.get("he") == he and r.get("ru") == ru and r.get("category") == cat:
            if not force:
                raise ValueError(f"exact duplicate of {r['id']}: {he}→{ru}")
    # Conflict (same he, different ru)?
    for r in rules:
        if r.get("he") == he and r.get("ru") != ru:
            if not force:
                raise ValueError(f"conflict with {r['id']}: he='{he}' already maps to '{r['ru']}'")

    rule_id = item.get("id") or next_id(rules, CAT_PREFIX[cat])
    return {
        "id": rule_id,
        "he": he,
        "ru": ru,
        "translit": item.get("translit"),
        "category": cat,
        "subcategory": item.get("subcat") or item.get("subcategory") or "",
        "priority": item.get("priority", 3),
        "context": item.get("context"),
        "source": item.get("source", "comparison"),
        "note": item.get("note", ""),
        "added_in_version": version,
        "added_at_article": item.get("article") or item.get("added_at_article"),
        "conflicts_with": [],
    }


def cmd_add(args, gdata, gpath):
    new = deepcopy(gdata)
    next_ver = f"{int(new['meta']['version']) + 1:03d}"
    item = {
        "he": args.he,
        "ru": args.ru,
        "cat": args.cat,
        "subcat": args.subcat or "",
        "translit": args.translit,
        "priority": args.priority or 3,
        "context": args.context,
        "source": args.source or "comparison",
        "note": args.note or "",
        "article": args.article,
        "id": args.id_override,
    }
    try:
        rule = _build_rule(item, new["rules"], next_ver, force=args.force)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("Use --force to override, or 'update' for existing rules.", file=sys.stderr)
        sys.exit(1)
    new["rules"].append(rule)
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"Added {rule['id']}: {args.he} → {args.ru}")


def _apply_update(rule: dict, patch: dict):
    for k in ("he", "ru", "translit", "note", "priority", "context", "subcategory", "conflicts_with"):
        if k in patch and patch[k] is not None:
            rule[k] = patch[k]


def cmd_update(args, gdata, gpath):
    if not any(x is not None for x in (args.he, args.ru, args.translit, args.note, args.priority, args.context)):
        print("ERROR: nothing to update (provide at least one field)", file=sys.stderr)
        sys.exit(1)
    new = deepcopy(gdata)
    target = next((r for r in new["rules"] if r["id"] == args.id.upper()), None)
    if not target:
        print(f"ERROR: rule {args.id} not found", file=sys.stderr)
        sys.exit(1)
    patch = {
        "he": args.he,
        "ru": args.ru,
        "translit": args.translit,
        "note": args.note,
        "priority": args.priority,
        "context": args.context,
    }
    _apply_update(target, patch)
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"Updated {args.id.upper()}")


def cmd_delete(args, gdata, gpath):
    new = deepcopy(gdata)
    if not any(r["id"] == args.id.upper() for r in new["rules"]):
        print(f"ERROR: rule {args.id} not found", file=sys.stderr)
        sys.exit(1)
    new["rules"] = [r for r in new["rules"] if r["id"] != args.id.upper()]
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"Deleted {args.id.upper()}")


def cmd_batch_add(args, gdata, gpath):
    """Пакетно добавить правила из JSON-файла (массив объектов)."""
    batch = json.loads(Path(args.file).read_text(encoding="utf-8"))
    new = deepcopy(gdata)
    next_ver = f"{int(new['meta']['version']) + 1:03d}"
    added = []
    errors = []
    for i, item in enumerate(batch):
        try:
            rule = _build_rule(item, new["rules"], next_ver, force=args.force)
            new["rules"].append(rule)
            added.append(rule["id"])
        except ValueError as e:
            errors.append(f"  item[{i}]: {e}")
    if errors:
        print(f"ERROR: {len(errors)} items rejected; no changes written.", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"Batch added {len(added)} rules: {', '.join(added)}")


def cmd_batch_update(args, gdata, gpath):
    """
    Атомарно выполнить несколько операций, результат — одна новая версия.

    Формат файла: список объектов с полем `op` ∈ {add, update, delete}.
    """
    ops = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(ops, list):
        print("ERROR: file must contain a JSON list of operations", file=sys.stderr)
        sys.exit(1)
    new = deepcopy(gdata)
    next_ver = f"{int(new['meta']['version']) + 1:03d}"
    log = []
    errors = []
    for i, op in enumerate(ops):
        kind = op.get("op")
        try:
            if kind == "add":
                rule = _build_rule(op, new["rules"], next_ver, force=op.get("force", False))
                new["rules"].append(rule)
                log.append(f"add {rule['id']}")
            elif kind == "update":
                target = next((r for r in new["rules"] if r["id"] == op["id"].upper()), None)
                if not target:
                    raise ValueError(f"rule {op['id']} not found")
                _apply_update(target, op)
                log.append(f"update {target['id']}")
            elif kind == "delete":
                rid = op["id"].upper()
                if not any(r["id"] == rid for r in new["rules"]):
                    raise ValueError(f"rule {rid} not found")
                new["rules"] = [r for r in new["rules"] if r["id"] != rid]
                log.append(f"delete {rid}")
            elif kind == "set-meta":
                for k, v in op.get("fields", {}).items():
                    new["meta"][k] = v
                log.append(f"set-meta {list(op.get('fields', {}).keys())}")
            else:
                raise ValueError(f"unknown op {kind!r}")
        except Exception as e:
            errors.append(f"  op[{i}] {kind}: {e}")
    if errors:
        print(f"ERROR: {len(errors)} operations failed; no changes written.", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"batch-update: {len(log)} operations applied ({out.name})")
    for e in log:
        print(" ", e)


def cmd_stats(args, gdata, gpath):
    rules = gdata["rules"]
    meta = gdata["meta"]
    print(f"Glossary: {gpath.name}")
    print(f"Version:  {meta['version']}")
    print(f"Rules:    {len(rules)}")
    print(f"Updated:  {meta.get('updated', '?')}")
    print(f"Articles: {meta.get('articles_processed', '?')} / {meta.get('total_articles', '?')}")
    print()
    cats = {}
    prios = {}
    for r in rules:
        cats[r.get("category", "?")] = cats.get(r.get("category", "?"), 0) + 1
        prios[r.get("priority", "?")] = prios.get(r.get("priority", "?"), 0) + 1
    for c, n in sorted(cats.items()):
        print(f"  {c}: {n}")
    print()
    for p, n in sorted(prios.items()):
        print(f"  priority {p}: {n}")


def cmd_conflicts(args, gdata, gpath):
    """Найти 6 типов конфликтов в словаре."""
    rules = gdata["rules"]
    id_set = {r["id"] for r in rules}
    issues = []  # (severity, kind, subject, ids)

    # 1. Одинаковый `he` + разный `ru`
    by_he = {}
    for r in rules:
        by_he.setdefault(r.get("he", ""), []).append(r)
    for he, grp in by_he.items():
        if len({r.get("ru", "") for r in grp}) > 1:
            issues.append(("HIGH", "IDENTICAL-HE-DIFF-RU", he, [r["id"] for r in grp]))

    # 2. Точные дубликаты (he + ru + category)
    by_key = {}
    for r in rules:
        k = (r.get("he", ""), r.get("ru", ""), r.get("category", ""))
        by_key.setdefault(k, []).append(r)
    for (he, ru, _), grp in by_key.items():
        if len(grp) > 1:
            issues.append(("MED", "EXACT-DUPLICATE", f"{he} → {ru}", [r["id"] for r in grp]))

    # 3. Пересекающиеся варианты (через /)
    # Документированные пары (взаимный `conflicts_with`) подавляются как известные.
    def mutual_cw(a_id, b_id):
        a = next((r for r in rules if r["id"] == a_id), None)
        b = next((r for r in rules if r["id"] == b_id), None)
        return (a and b and b_id in (a.get("conflicts_with") or [])
                and a_id in (b.get("conflicts_with") or []))

    variant_owners = {}
    for r in rules:
        if r.get("category") != "terminological":
            continue
        for v in split_variants(r.get("he", "")):
            variant_owners.setdefault(v, []).append(r["id"])
    for v, owners in variant_owners.items():
        if len(owners) > 1:
            # Suppress if all pairs are mutually documented as related
            all_documented = all(
                mutual_cw(owners[i], owners[j])
                for i in range(len(owners))
                for j in range(i + 1, len(owners))
            )
            if all_documented:
                issues.append(("INFO", "SHARED-VARIANT-DOCUMENTED", v, owners))
            else:
                issues.append(("HIGH", "SHARED-VARIANT", v, owners))

    # 4. Битые conflicts_with
    for r in rules:
        for cid in r.get("conflicts_with") or []:
            if cid not in id_set:
                issues.append(("LOW", "BROKEN-CONFLICTS-WITH", cid, [r["id"]]))

    # 5. Stale ID references in ru / note (mention of deleted rule)
    for r in rules:
        for field in ("ru", "note"):
            text = r.get(field, "") or ""
            for m in ID_REF_RE.finditer(text):
                ref = m.group(1)
                if ref not in id_set and ref != r["id"]:
                    issues.append(("LOW", f"STALE-REF-IN-{field.upper()}", ref, [r["id"]]))

    # 6. (опц) substring-overlap; только c --strict
    if args.strict:
        term_variants = []
        for r in rules:
            if r.get("category") != "terminological":
                continue
            for v in split_variants(r.get("he", "")):
                term_variants.append((v, r["id"]))
        seen_pairs = set()
        for i, (v1, id1) in enumerate(term_variants):
            if len(v1) < 4:
                continue
            for v2, id2 in term_variants[i + 1 :]:
                if id1 == id2 or v1 == v2:
                    continue
                if v1 in v2 or v2 in v1:
                    pair = tuple(sorted([id1, id2]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    shorter, longer = (v1, v2) if len(v1) < len(v2) else (v2, v1)
                    issues.append(("INFO", "SUBSTRING-OVERLAP", f"{shorter} ⊂ {longer}", list(pair)))

    if not issues:
        print("No conflicts found.")
        return 0

    # Group by severity
    by_sev = {}
    for sev, kind, subj, ids in issues:
        by_sev.setdefault(sev, []).append((kind, subj, ids))
    order = ["HIGH", "MED", "LOW", "INFO"]
    for sev in order:
        grp = by_sev.get(sev, [])
        if not grp:
            continue
        print(f"\n=== {sev} ({len(grp)}) ===")
        for kind, subj, ids in grp:
            print(f"  [{kind}] {subj}  ({', '.join(ids)})")
    print(f"\n--- Total: {len(issues)} issues ---")
    return 1 if any(s in ("HIGH", "MED") for s, *_ in issues) else 0


def cmd_validate(args, gdata, gpath):
    """Проверить схему словаря и ссылочную целостность."""
    errors = []
    warnings = []
    rules = gdata.get("rules", [])
    id_set = {r.get("id") for r in rules}
    seen = set()
    for r in rules:
        rid = r.get("id") or "<no-id>"
        for f in REQUIRED_RULE_FIELDS:
            if f not in r or r[f] in (None, ""):
                errors.append(f"{rid}: missing/empty required field `{f}`")
        if rid in seen:
            errors.append(f"{rid}: duplicate ID")
        seen.add(rid)
        cat = r.get("category")
        if cat and cat not in VALID_CATEGORIES:
            errors.append(f"{rid}: invalid category `{cat}`")
        if r.get("priority") not in VALID_PRIORITIES:
            errors.append(f"{rid}: invalid priority `{r.get('priority')}` (must be 1..5)")
        # id prefix: T=terminological, X=syntactic; S/Y both historically used for semantic/stylistic.
        if cat:
            first = rid[:1] if rid else ""
            if first not in ("T", "S", "X", "Y"):
                errors.append(f"{rid}: ID prefix must be one of T/S/X/Y")
            elif cat == "terminological" and first != "T":
                warnings.append(f"{rid}: terminological rule should start with T")
            elif cat == "syntactic" and first != "X":
                warnings.append(f"{rid}: syntactic rule should start with X")
            # semantic/stylistic — both S and Y accepted (historical convention)
        # broken refs
        for cid in r.get("conflicts_with") or []:
            if cid not in id_set:
                warnings.append(f"{rid}: broken conflicts_with→{cid} (rule not present)")

    meta = gdata.get("meta", {})
    if "version" not in meta:
        errors.append("meta.version missing")
    if meta.get("rules_count") and meta["rules_count"] != len(rules):
        warnings.append(f'meta.rules_count={meta["rules_count"]} ≠ actual {len(rules)}')

    print(f"{gpath.name}: {len(errors)} errors, {len(warnings)} warnings")
    for e in errors:
        print(" ERR ", e)
    for w in warnings:
        print(" WARN", w)
    return 1 if errors else 0


def cmd_diff(args, gdata, gpath):
    """Сравнить две версии словаря."""
    base_dir = gpath.parent
    a_path = base_dir / f"glossary_{int(args.from_ver):03d}.json"
    b_path = base_dir / f"glossary_{int(args.to_ver):03d}.json"
    if not a_path.exists():
        print(f"ERROR: {a_path} not found", file=sys.stderr)
        sys.exit(1)
    if not b_path.exists():
        print(f"ERROR: {b_path} not found", file=sys.stderr)
        sys.exit(1)
    a = load_glossary(a_path)
    b = load_glossary(b_path)
    a_by_id = {r["id"]: r for r in a["rules"]}
    b_by_id = {r["id"]: r for r in b["rules"]}

    added = [rid for rid in b_by_id if rid not in a_by_id]
    removed = [rid for rid in a_by_id if rid not in b_by_id]
    changed = []
    for rid in sorted(set(a_by_id) & set(b_by_id)):
        ra, rb = a_by_id[rid], b_by_id[rid]
        diffs = {k: (ra.get(k), rb.get(k)) for k in set(ra) | set(rb) if ra.get(k) != rb.get(k)}
        if diffs:
            changed.append((rid, diffs))

    print(f"=== {a_path.name} → {b_path.name} ===")
    print(f"added:   {len(added)}")
    print(f"removed: {len(removed)}")
    print(f"changed: {len(changed)}")
    if added:
        print("\n-- added --")
        for rid in sorted(added):
            r = b_by_id[rid]
            print(f"  + {rid}  {r.get('he','')[:40]} → {r.get('ru','')[:60]}")
    if removed:
        print("\n-- removed --")
        for rid in sorted(removed):
            r = a_by_id[rid]
            print(f"  − {rid}  {r.get('he','')[:40]} → {r.get('ru','')[:60]}")
    if changed and args.verbose:
        print("\n-- changed --")
        for rid, diffs in changed:
            print(f"  ~ {rid}")
            for k, (av, bv) in diffs.items():
                print(f"      {k}: {av!r} → {bv!r}")
    elif changed:
        print("\n-- changed (use --verbose for field-level diff) --")
        for rid, _ in changed[:20]:
            print(f"  ~ {rid}")
        if len(changed) > 20:
            print(f"  ... and {len(changed) - 20} more")


def cmd_dump(args, gdata, gpath):
    ids = [x.strip().upper() for x in args.ids.split(",")]
    results = [r for r in gdata["rules"] if r["id"] in ids]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    missing = set(ids) - {r["id"] for r in results}
    if missing:
        print(f"Not found: {', '.join(missing)}", file=sys.stderr)


def cmd_version(args, gdata, gpath):
    meta = gdata["meta"]
    print(
        f"{gpath.name}: v{meta['version']}, "
        f"{meta.get('rules_count', len(gdata['rules']))} rules, "
        f"updated {meta.get('updated', '?')}"
    )


def cmd_set_meta(args, gdata, gpath):
    new = deepcopy(gdata)
    touched = False
    if args.articles_processed is not None:
        new["meta"]["articles_processed"] = args.articles_processed
        touched = True
    if args.note_key:
        if args.note_value is None:
            print("ERROR: --note-key requires --note-value", file=sys.stderr)
            sys.exit(1)
        new["meta"][args.note_key] = args.note_value
        touched = True
    if not touched:
        print("ERROR: nothing to set", file=sys.stderr)
        sys.exit(1)
    bump_meta(new)
    out = next_version_path(gpath)
    save_glossary(new, out)
    print(f"Meta updated, saved as {out.name}")


# ── Main ─────────────────────────────────────────────────────────────


def main():
    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "glossary"

    parser = argparse.ArgumentParser(description="Zohar glossary CLI tool")
    parser.add_argument("--glossary", type=Path, default=None, help="Path to glossary JSON (default: <repo>/glossary/glossary.json)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("lookup", help="Find rules matching Hebrew text")
    p.add_argument("--text", help="Path to Hebrew text file")
    p.add_argument("--stdin", action="store_true", help="Read from stdin")
    p.add_argument("--paragraphs", help="Path to source JSON (e.g. Introduction.json)")
    p.add_argument("--range", nargs=2, type=int, metavar=("START", "END"), help="Paragraph range (1-based inclusive)")
    p.add_argument("--compact", "-c", action="store_true")
    p.add_argument("--raw", action="store_true", help="Disable boundary/conflict filters (old behavior)")
    p.add_argument("--no-suffix", action="store_true", help="Disable Hebrew/Aramaic noun-suffix matching (ים/ין/ות/א/יא/ייא)")

    p = sub.add_parser("search", help="Search rules")
    p.add_argument("--he")
    p.add_argument("--ru")
    p.add_argument("--cat")
    p.add_argument("--subcat")
    p.add_argument("--id")
    p.add_argument("--compact", "-c", action="store_true")

    p = sub.add_parser("add", help="Add one rule")
    p.add_argument("--he", required=True)
    p.add_argument("--ru", required=True)
    p.add_argument("--cat", required=True, choices=list(CAT_PREFIX))
    p.add_argument("--subcat", default="")
    p.add_argument("--translit", default=None)
    p.add_argument("--priority", type=int, default=3)
    p.add_argument("--context", default=None)
    p.add_argument("--source", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--article", type=int, default=None)
    p.add_argument("--id-override", default=None)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("update", help="Update one rule")
    p.add_argument("--id", required=True)
    p.add_argument("--he", default=None)
    p.add_argument("--ru", default=None)
    p.add_argument("--translit", default=None)
    p.add_argument("--note", default=None)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--context", default=None)

    p = sub.add_parser("delete", help="Delete one rule")
    p.add_argument("--id", required=True)

    p = sub.add_parser("batch-add", help="Batch-add rules from a JSON array file")
    p.add_argument("--file", required=True)
    p.add_argument("--force", action="store_true")
    # alias `batch`
    p2 = sub.add_parser("batch", help="(alias for batch-add)")
    p2.add_argument("--file", required=True)
    p2.add_argument("--force", action="store_true")

    p = sub.add_parser("batch-update", help="Atomic multi-op (add/update/delete/set-meta)")
    p.add_argument("--file", required=True, help="JSON array of operations with `op` field")

    sub.add_parser("stats", help="Summary statistics")

    p = sub.add_parser("conflicts", help="Find conflicts (6 checks)")
    p.add_argument("--strict", action="store_true", help="Also include SUBSTRING-OVERLAP (noisy)")

    sub.add_parser("validate", help="Validate schema and referential integrity")

    p = sub.add_parser("diff", help="Compare two versions")
    p.add_argument("--from", dest="from_ver", required=True, help="Version NNN to compare from")
    p.add_argument("--to", dest="to_ver", required=True, help="Version NNN to compare to")
    p.add_argument("--verbose", "-v", action="store_true")

    p = sub.add_parser("dump", help="Print full JSON of specific rules")
    p.add_argument("--ids", required=True)

    sub.add_parser("version", help="Print version info")

    p = sub.add_parser("set-meta", help="Update meta fields")
    p.add_argument("--articles-processed", type=int, default=None)
    p.add_argument("--note-key", default=None)
    p.add_argument("--note-value", default=None)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    gpath = args.glossary or find_latest_glossary(base)
    gdata = load_glossary(gpath)

    dispatch = {
        "lookup": cmd_lookup,
        "search": cmd_search,
        "add": cmd_add,
        "update": cmd_update,
        "delete": cmd_delete,
        "batch-add": cmd_batch_add,
        "batch": cmd_batch_add,  # alias
        "batch-update": cmd_batch_update,
        "stats": cmd_stats,
        "conflicts": cmd_conflicts,
        "validate": cmd_validate,
        "diff": cmd_diff,
        "dump": cmd_dump,
        "version": cmd_version,
        "set-meta": cmd_set_meta,
    }
    rc = dispatch[args.command](args, gdata, gpath)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
