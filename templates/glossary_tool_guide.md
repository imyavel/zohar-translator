# Инструкция по работе со словарём

> **Это руководство по работе со словарём через CLI-тулзу
> `corpus_tools/glossary_tool.py`.** Структура самого файла
> `glossary/glossary.json` — заточена под иврит+арамит+гершаимы → русский
> (Книга Зоар). Для другого языка-источника или другой предметной
> области нужна адаптация: структура полей записи, команды
> `find`/`add`, возможно сам `glossary_tool.py`. Это делается на
> стадии 4 (см. `stages/04_glossary.md`).
>
> Этот файл — полная инструкция по CLI для словаря.
> Загружать вместо чтения `glossary.json` напрямую.

## Расположение

- **Словарь:** `glossary/glossary.json` в репо zohar-translator.
  Версионирование — через git (история коммитов = история словаря).
- **Утилита:** `corpus_tools/glossary_tool.py`.
- **Переменные путей**, используемые ниже:
  - `$HEB_ROOT` — рабочая директория корпуса оператора
    (где `Source/`, `Translated/`, и при загрузке через `download_sefaria.py`
    туда же кладётся локальный кэш каталога).
  - `$CORPUS_TOOLS` — `<repo>/corpus_tools/`.

## Главное правило

**НИКОГДА не читать `glossary.json` целиком через Read/cat/open.**
Словарь ~335 КБ (~70k токенов). Все операции — через утилиту.

---

## Команды

Запуск из `$HEB_ROOT`:
```
cd $HEB_ROOT && python $CORPUS_TOOLS/glossary_tool.py <команда>
```

### 1. `lookup` — релевантные правила для ивр.-текста

**Когда:** перед переводом статьи/чанка. Основной канал получения словарных правил.

```bash
# По диапазону параграфов источника (рекомендуется):
python $CORPUS_TOOLS/glossary_tool.py lookup \
  --paragraphs Source/Sulam_on_Zohar/<Section>.json \
  --range <START> <END> -c

# По файлу с ивр.-текстом:
python $CORPUS_TOOLS/glossary_tool.py lookup --text path/to/hebrew.txt -c
```

Флаг `-c` / `--compact` — одна строка на правило (формат: `ID [кат] he → ru (pN)`).
Без `-c` — полный JSON.

**Логика матчинга и фильтрации (обновлено в v106):**

* Термины ловятся **по границам слова**, а не по произвольной подстроке:
  слева/справа должен стоять не-ивр. символ (пунктуация, пробел, латиница).
  Для одиночных ивр. слов разрешены слитные префиксы `ה/ב/ל/ש/ו/מ/כ`.
  Фразы (с пробелом) и аббревиатуры (с `"`) — строгий матч без префиксов.
* **Суффиксный матчинг** (для одиночных ивр.-слов >= 3 букв): на правой
  границе допустимы словоизменительные суффиксы `ים` / `ין` / `ות` / `א`
  / `יא` / `ייא`. При наслоении суффикса финальные буквы основы
  (`ך/ם/ן/ף/ץ`) автоматически расфинализируются (`ן→נ` и т.д.). Так
  T005 (`דין` → «суд») ловит `דינים`, `דינין`, `דינא`, `דיניא`, `דינייא`.
  Отключается флагом `--no-suffix` (отладка/ревизии).
* Если на одной и той же ивр.-подстроке сработали два `terminological`-правила
  с разными приоритетами — оставляется с **высшим** (priority=1 > priority=2).
  При равных приоритетах в stderr появляется `WARN`.
* Если правило A матчится и в `conflicts_with` A указано правило B (тоже в матчах),
  и priority(A) < priority(B) — B подавляется.
* `semantic / syntactic / stylistic` — инструкции, всегда включаются.

**Флаг `--raw`** отключает эту фильтрацию (старое substring-поведение). Использовать
только для отладки/ревизий, **не** для боевого перевода.

Stderr показывает сводку: `N / M rules matched (kept_term/raw_term after resolved)`.
Если `kept_term < raw_term` — подавлены конфликтующие / ложно-срабатывающие правила.

### 2. `search` — найти конкретные правила

```bash
python $CORPUS_TOOLS/glossary_tool.py search --he "מלכות" -c      # по иврит-тексту
python $CORPUS_TOOLS/glossary_tool.py search --ru "Малхут" -c     # по русскому
python $CORPUS_TOOLS/glossary_tool.py search --cat semantic -c    # по категории
python $CORPUS_TOOLS/glossary_tool.py search --id T042 -c         # по ID
```

`search` — substring-match без word-boundary (это быстрый поиск, не lookup).

### 3. `add` — добавить одно правило

```bash
python $CORPUS_TOOLS/glossary_tool.py add \
  --he "ивритский термин" \
  --ru "русский перевод" \
  --cat terminological \
  --subcat terms \
  --translit "транслитерация" \
  --priority 2 \
  --source "comparison:16" \
  --note "Обоснование" \
  --article 15
```

- `--cat`: `terminological | semantic | syntactic | stylistic`
- `--priority`: `1`=безусловное … `5`=рекомендация
- `--force`: добавить поверх конфликта (иначе — ошибка)

**Защита (обновлено в v106):** скрипт отказывается добавить:
- точный дубликат (`he + ru + category` уже есть);
- конфликт (тот же `he`, другой `ru`).
В обоих случаях сначала предлагается `--force` или `update`.

### 4. `batch-add` — пакетное добавление (одна новая версия на всю пачку)

Файл `_tmp_batch.json`:
```json
[
  {"he":"...","ru":"...","cat":"terminological","subcat":"terms","priority":2,
   "source":"comparison:16","article":16},
  {"he":"...","ru":"...","cat":"semantic","subcat":"concept","priority":3,
   "source":"comparison:16","article":16}
]
```

```bash
python $CORPUS_TOOLS/glossary_tool.py batch-add --file _tmp_batch.json
```

Старое название `batch` сохранено как алиас.

### 5. `batch-update` — **атомарно** применить смесь add/update/delete

**Когда:** ревизии, рефакторинг словаря, применение набора исправлений
как единой транзакции.

Файл `_tmp_ops.json`:
```json
[
  {"op":"update","id":"T001","ru":"новое значение","note":"причина"},
  {"op":"delete","id":"S039"},
  {"op":"add","he":"...","ru":"...","cat":"stylistic","priority":1},
  {"op":"set-meta","fields":{"v107_changes":"сводка правок"}}
]
```

```bash
python $CORPUS_TOOLS/glossary_tool.py batch-update --file _tmp_ops.json
```

Если **любая** операция падает (правила нет, конфликт при `add`, и т. п.) — **ни одна** правка не сохраняется. Успешный прогон — один новый коммит в `glossary/glossary.json`.

### 6. `update` / `delete` — одноразовые

```bash
python $CORPUS_TOOLS/glossary_tool.py update --id T042 --ru "..." --note "..."
python $CORPUS_TOOLS/glossary_tool.py delete --id T042
```

Для многих правок предпочтительнее `batch-update` (одна версия вместо многих).

### 7. `stats` — сводка

```bash
python $CORPUS_TOOLS/glossary_tool.py stats
```

Показывает версию, число правил, раскладки по категориям и приоритетам.

### 8. `conflicts` — найти конфликты (6 проверок)

**Когда:** ревизии словаря, до/после крупных правок.

```bash
python $CORPUS_TOOLS/glossary_tool.py conflicts           # основные проверки
python $CORPUS_TOOLS/glossary_tool.py conflicts --strict  # + SUBSTRING-OVERLAP (шумно)
```

Находит:
- **HIGH** `IDENTICAL-HE-DIFF-RU` — один `he`, разные `ru`.
- **HIGH** `SHARED-VARIANT` — два T-правила делят вариант через `/`.
- **MED** `EXACT-DUPLICATE` — полные дубликаты (`he + ru + category`).
- **LOW** `BROKEN-CONFLICTS-WITH` — ссылка на несуществующий ID.
- **LOW** `STALE-REF-IN-RU` / `STALE-REF-IN-NOTE` — упоминание удалённого ID в тексте.
- (с `--strict`) **INFO** `SUBSTRING-OVERLAP` — один термин есть внутри другого (≥ 4 симв.).

Exit-code: `1` при наличии HIGH/MED, иначе `0`.

### 9. `validate` — проверить схему и ссылочную целостность

```bash
python $CORPUS_TOOLS/glossary_tool.py validate
```

Проверяет: обязательные поля, допустимые категории/приоритеты, уникальность ID,
префикс ID по категории (T/X строго; S/Y — оба допустимы для semantic/stylistic),
битые `conflicts_with`, согласованность `meta.rules_count`.

Exit-code: `1` при errors.

### 10. `diff` — сравнить две версии

> **Примечание.** В этом репо словарь — один файл `glossary/glossary.json`,
> история ведётся через git. Команда `diff` ниже работает в legacy-режиме,
> если рядом со словарём лежат старые `glossary_NNN.json`. Для сравнения
> произвольных коммитов используй:
>
> ```bash
> git show HEAD~5:glossary/glossary.json > /tmp/old.json
> python $CORPUS_TOOLS/glossary_tool.py diff --from-file /tmp/old.json --to-file glossary/glossary.json
> ```
>
> (флаги `--from-file/--to-file` — на TODO; на момент B1 ещё не реализованы.)

```bash
python $CORPUS_TOOLS/glossary_tool.py diff --from 100 --to 106
python $CORPUS_TOOLS/glossary_tool.py diff --from 100 --to 106 --verbose   # с полями
```

Показывает added / removed / changed правила между версиями.

### 11. `dump` — полный JSON выбранных правил

```bash
python $CORPUS_TOOLS/glossary_tool.py dump --ids T001,S005,X001
```

### 12. `set-meta` — метаданные

```bash
python $CORPUS_TOOLS/glossary_tool.py set-meta --articles-processed 16
python $CORPUS_TOOLS/glossary_tool.py set-meta --note-key "article_16_notes" --note-value "..."
```

В `batch-update` то же самое делается операцией `{"op":"set-meta","fields":{...}}`.

### 13. `version`

```bash
python $CORPUS_TOOLS/glossary_tool.py version
```

---

## Рабочий цикл для статьи N

| Шаг | Что делать со словарём |
|-----|----------------------|
| B0 (перевод без словаря) | Словарь не используется |
| B (перевод со словарём) | `lookup --paragraphs ... --range S E -c` → использовать вывод как контекст |
| C (загрузить BB) | Словарь не используется |
| D (сравнение с BB) | `search` по конкретным терминам при расхождениях |
| E (обновление) | `batch-update` со списком добавлений/правок, затем `set-meta --articles-processed N` (или отдельной op в том же batch) |
| Ревизия | `conflicts` + `validate` + `stats` |

## Версионирование

- Каждая мутация (`add`/`update`/`delete`/`batch-*`/`set-meta`) пишет
  прямо в `glossary/glossary.json`. История — через
  `git log -p glossary/glossary.json` и `git blame`.
- Legacy-режим с `glossary_NNN.json` в публичной сборке **не
  используется**. Если рядом со словарём оказались старые
  numbered-файлы (миграция из приватной сборки),
  `find_latest_glossary` сначала выберет `glossary.json`, если он
  существует, и только при его отсутствии откатится на старший NNN —
  это режим совместимости, не основная модель.
- Команды `diff --from N --to M` в §10 — **legacy**. Для актуальной
  сборки сравнение версий делается через
  `git diff <commit1> <commit2> -- glossary/glossary.json`.

## Типичные сценарии

### Добавить 5 правил из сравнения со статьёй 17

```bash
cat > _tmp_batch.json <<EOF
[
  {"he":"...","ru":"...","cat":"terminological","priority":2,"source":"comparison:17","article":17},
  ... (ещё 4)
]
EOF
python $CORPUS_TOOLS/glossary_tool.py batch-add --file _tmp_batch.json
rm _tmp_batch.json
```

### Переписать правило и снять пометку articles_processed одновременно

```bash
cat > _tmp_ops.json <<EOF
[
  {"op":"update","id":"T042","ru":"новое","note":"обоснование"},
  {"op":"set-meta","fields":{"articles_processed":17}}
]
EOF
python $CORPUS_TOOLS/glossary_tool.py batch-update --file _tmp_ops.json
rm _tmp_ops.json
```

### Аудит перед релизом

```bash
python $CORPUS_TOOLS/glossary_tool.py validate
python $CORPUS_TOOLS/glossary_tool.py conflicts
python $CORPUS_TOOLS/glossary_tool.py stats
```

Все три должны выйти с кодом 0 и без HIGH/MED.
