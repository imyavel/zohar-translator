# Каталоги корпуса: `catalog.json` и `articles_catalog.json`

Этот документ описывает два «каталожных» файла, на которые опирается
вся pipeline переводчика, и объясняет, как они связаны с единицами
чанкования orchestrator'а. Сами файлы **в репо не лежат**: они derived
из Sefaria (CC BY 4.0; для Сулама лицензия неоднозначна, поэтому мы их
не публикуем). Оператор пересобирает их сам — `catalog.json` через
`download_sefaria.py`, `articles_catalog.json` — отдельным шагом
адаптации под свой корпус.

Документ нужен, чтобы:
1. Объяснить, что именно генерирует `download_sefaria.py` и зачем.
2. Зафиксировать схему `articles_catalog.json`, которую ожидают
   `corpus_tools/build_batch.py`, `next_cursor.py`, `mark_article_done.py`
   и шаблон промпта.
3. Объяснить, как cатлог соединяется с режимами orchestrator
   (батчи → статьи → чанки).

---

## 1. `<HEB_ROOT>/Source/catalog.json` — что генерирует `download_sefaria.py`

Создаётся автоматически в конце прогона `download_sefaria.py`.
Inventory загруженных секций. Используется как справочник «что вообще
есть в корпусе».

Формат:

```json
{
  "project": "Зоар-Сулам → Русский → Wikibooks",
  "source": "sefaria.org",
  "download_date": "2026-04-12T16:18:39.292908",
  "works": [
    {
      "work": "Sulam on Zohar",
      "ref": "Sulam_on_Zohar",
      "sections": [
        { "section": "Introduction", "paragraphs": 1381,
          "ref": "Sulam on Zohar, Introduction" },
        { "section": "Bereshit_I",  "paragraphs": 1060, ... },
        ...
      ],
      "total_paragraphs": 15711
    },
    { "work": "Zohar", ... },
    { "work": "Zohar Chadash", ... },
    { "work": "Tikkunei Zohar", ... }
  ],
  "total_paragraphs_all_works": <N>
}
```

Параллельно каждая секция выкладывается на диск как
`<HEB_ROOT>/Source/<Work>/<Section>.json` в формате Sefaria API:
поля `he` (массив параграфов на иврите/арамите) и `text` (английский
перевод, если есть). Именно эти файлы потом читает translator-агент.

## 2. `<HEB_ROOT>/Source/articles_catalog.json` — единица планирования

**Этот файл — ключевая адаптация под наш корпус.** `download_sefaria.py`
его НЕ создаёт; он составляется отдельно (для Зоара мы делали его
вручную + полу-автоматически на основе традиционной разбивки параш
Сулама на статьи). Без него orchestrator не знает, что считать «одной
статьёй» — единицей перевода и единицей завершения.

Формат — массив записей, по одной на статью:

```json
[
  {
    "chapter":            "Introduction",
    "volume":             1,
    "article_index":      1,
    "he_title":           "השושנה",
    "en_label":           "Introduction",
    "zohar_paragraphs":   [1, 3],
    "sulam_paragraphs":   [1, 3],
    "sulam_section":      "Introduction",
    "source_chars":       10473,
    "book":               "Введение (Акдама)",
    "book_index":         0,
    "chapter_ru":         "Акдама (Предисловие)",
    "chapter_order":      1
  },
  ...
]
```

Семантика полей:

| Поле | Что значит |
|---|---|
| `chapter` | Английский slug главы (parasha). Используется как ключ в `next_cursor.py` и в именах файлов прогресса. |
| `chapter_ru` | Русское имя главы (для UI, имени поддиректории `Translated/`, заголовков). |
| `book`, `book_index` | Том (для иерархии «том → глава → статья» в сайт-сборщике). |
| `chapter_order` | Глобальный порядок глав в корпусе (по нему сортируется next_cursor). |
| `article_index` | Глобальный сквозной номер статьи (используется в имени файла `NNN.md` и в логах). |
| `he_title` | Заголовок статьи на иврите (показывается в шаблоне промпта). |
| `sulam_section` | Имя секции в `Source/Sulam_on_Zohar/<sulam_section>.json` — туда смотрит переводчик. |
| `sulam_paragraphs` | `[start, end]` — диапазон параграфов секции, образующий эту статью. Этот же диапазон попадает в `{{start}}/{{end}}` шаблона промпта. |
| `source_chars` | Сумма длин параграфов в символах. Используется планировщиком батчей (`build_batch.py`) для оценки токенов. |
| `zohar_paragraphs` | Аналог для исходного Зоара (без Сулама). Для нашего основного pipeline не критично. |
| `volume`, `en_label` | Информационные. |

## 3. Как каталог соединяется с orchestrator (единицы чанкования)

Иерархия в нашей системе **трёхуровневая**:

```
Глава (chapter)
  └─ Статья (article)              ← единица завершения, файл NNN.md
       └─ Чанк (chunk)              ← единица одного вызова translator-агента
```

**Глава** — это единица планирования и push'а на сайт. `next_cursor.py`
выбирает следующую главу со статусом «не все статьи готовы», и весь
батч orchestrator'а формируется внутри неё (или нескольких подряд, пока
не упрётся в `--usage` лимит).

**Статья** — это единица, которую orchestrator считает «выполненной».
`mark_article_done.py` фиксирует её в `<chapter>/progress.json`, и
auto-deploy (`gh_deploy.py`) триггерится именно на `article_done`.
Граница статьи задаётся `sulam_paragraphs: [start, end]` в каталоге.

**Чанк** — это уже внутри translator-агента. Если в статье много
параграфов и она не вмещается в `CHUNK_BUDGET_CHARS` (default ≈ 7500
символов исходника), translator делит её на куски сам, по инструкции
из `translation_prompt.md` (Шаг 4). Каждый чанк дописывается в один
итоговый `NNN.md` под общим заголовком статьи. Если translator упёрся
в hit-limit посреди статьи, `partial_state.py` детектит частично
переведённую `.md`, и при resume orchestrator передаёт следующему
translator'у блок «продолжай с параграфа K».

`build_batch.py` читает `articles_catalog.json`, выбирает подмножество
статей (по `chapter_order` начиная с курсора, пока укладывается в
бюджет), рендерит для каждой `prompt_NNN_<chapter>.txt` через
`translation_prompt.md` и кладёт всё в `.batch/`. Дальше bash-launcher
последовательно (или параллельно — `PARALLEL_TRANSLATORS`) вызывает
`claude -p < prompt_NNN.txt`.

## 4. Что нужно адаптировать оператору под чужой корпус

1. **Свой источник** — заменить `download_sefaria.py` собственным
   загрузчиком (или взять готовый, если корпус есть на Sefaria).
   Выход — те же `.json` секций под `<HEB_ROOT>/Source/<...>/`.
2. **Свой `articles_catalog.json`** — главное. Решить, что у вас
   «глава», что «статья». Сохранить набор полей, перечисленных в §2
   (на них смотрят `build_batch.py`, `next_cursor.py`, шаблон промпта,
   `build_site.py`).
3. **Шаблон промпта** — переписать `templates/translation_prompt.md`
   под пары языков и стиль. Переменные `{{NN}}`, `{{chapter_ru}}`,
   `{{book}}`, `{{sulam_section}}`, `{{start}}`, `{{end}}`,
   `{{he_title}}`, `{{chapter_dir}}`, `{{HEB_ROOT}}`,
   `{{CORPUS_TOOLS}}`, `{{resume_block}}`, `{{chunk_budget_chars}}` —
   сохранить (их подставляет `build_batch.py`).
4. **Словарь** — `glossary/glossary.json` сейчас заточен под
   иврит+арамит+Зоар. Структура полей и методология использования
   через `glossary_tool.py` — переносимы; набор правил — нет
   (см. `templates/glossary_tool_guide.md`).

Все эти шаги ведёт LLM-агент через `RUN_ME.md` / `stages/02_source_loader.md`
и `stages/03_text_structure.md`.
