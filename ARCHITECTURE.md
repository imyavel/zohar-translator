# Архитектура

Документ объясняет, как устроено ядро zohar-translator и почему — на
уровне design rationale, а не построчного описания кода. Для запуска
смотри [`README.md`](README.md), для пошаговой адаптации под свой
корпус — [`RUN_ME.md`](RUN_ME.md) (фаза C плана).

---

## 1. Общая схема

```
┌─────────────────────────────────────────────────────────────────┐
│                          src/main.py                            │
│         поднимает Orchestrator (+ опц. Telegram bot)            │
│                  в одном asyncio loop                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │
        ┌──────────────────────┴─────────────────────────┐
        │                                                 │
┌───────▼────────┐                            ┌──────────▼────────┐
│  Orchestrator  │                            │      Bot          │
│ (orchestrator.py)│                          │   (bot.py, опц.)  │
│                │                            │                   │
│  FSM:          │  emit(event) ───────────►  │  /run /stop /push │
│  IDLE→PREPARING│                            │  pinned dashboard │
│   →RUNNING     │                            │  auto-deploy GH   │
│   →FINALIZING  │                            └───────────────────┘
│   →IDLE        │
└──┬─────────────┘
   │ spawn N
   │ subprocess
   ▼
┌──────────────────────────────────────────────────────────────────┐
│   N параллельных translator-агентов: claude.exe -p < prompt_NNN  │
│      (каждый — отдельный процесс, не in-process subagent)        │
└──────────────────────────────────────────────────────────────────┘
   │
   │ читает Source/<...>.json, пишет Translated/<book>/<chapter>/<NNN>.md
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ corpus_tools/build_site.py → Translated/Site/ (HTML + pagefind)  │
│ src/gh_deploy.py            → git push в GH Pages                │
└──────────────────────────────────────────────────────────────────┘
```

## 2. FSM orchestrator

Полностью реализован в `src/orchestrator.py`. Состояния и переходы:

```
       ┌──────────────────────────── manual /stop, /kill
       │                                       ▼
   ┌───┴────┐ wake / /run    ┌───────────┐  ok    ┌─────────┐
   │  IDLE  ├───────────────►│ PREPARING ├───────►│ RUNNING │
   └───┬────┘                └─────┬─────┘        └────┬────┘
       │                            │ next_cursor=null │
       │                            ▼                  ▼
       │                       (нечего делать)    FINALIZING
       │                                                │
       │                       ┌────────────────────────┘
       │                       │
       │  hit_limit_wait /     │
       │  idle (manual/fresh)  ▼
       └──────────────────  IDLE
                              ▲
                              │ DONE / ERROR ─► (state stays here
                                                  до явного /run)
```

- **IDLE** — fallthrough-состояние. У него есть `idle_reason`:
  `fresh` (только запустились), `manual` (оператор остановил),
  `hit_limit_wait` (ждём reset Anthropic'а; `next_wake_at` —
  timestamp). Из IDLE выход — по `_wake_event` (вручную через
  `/run`/`/resume` или по таймеру).
- **PREPARING** — `corpus_tools/next_cursor.py` определяет, какую главу
  переводить дальше; `build_batch.py` нарезает её на статьи и пишет
  `prompt_NNN_<chapter>.txt` в `.batch/` (плюс манифест). Если
  next_cursor пустой → переходим в DONE.
- **RUNNING** — главная фаза, см. §3.
- **FINALIZING** — `process_results.py` собирает результаты, фиксирует
  `last_session.json`, решает, что делать дальше: новый цикл, IDLE с
  reset-таймером, или ERROR.
- **DONE / ERROR** — терминальные. DONE = «корпус переведён», ERROR =
  «не понял, что произошло, нужна оператора рука».

Сериализация состояния — `state/current_run.json` (через
`src/state.py`), событий — `state/events.jsonl`. Bot читает оба
файла для dashboard'а.

## 3. Параллелизм translator'ов

В фазе RUNNING orchestrator поднимает пул из `PARALLEL_TRANSLATORS`
subprocess'ов и ждёт первый завершившийся через
`asyncio.wait(..., return_when=FIRST_COMPLETED)` (`orchestrator.py:529`).
Как только один translator закончил — обрабатываем его результат
(`_handle_translator_result`, ~line 680), и если ещё есть статьи и нет
«жёсткого» события — допускаем следующий до восстановления пула.

«Жёсткие» события, отменяющие весь in-flight пул:
- `hit_limit` (translator вернул `marker_rc=2`),
- `fatal_translator_error` (marker_rc вне {0, 1, 2, 3, 4}),
- `translator_timeout` (subprocess не уложился в
  `TRANSLATOR_TIMEOUT_SECONDS`),
- `consecutive_failures` (более N fail'ов подряд без single success).

При любом из них pending tasks отменяются и FSM уходит в FINALIZING.

**Anti-spam адаптирован под параллельный режим.** В sequential было
«N consecutive failures», в parallel — `total_failures_without_success`,
обнуляющийся на каждом success. Threshold —
`CONSECUTIVE_FAILURE_THRESHOLD` (default 5). Плюс burst-detection:
если `BURST_THRESHOLD` fail'ов укладываются в окно
`BURST_WINDOW_SECONDS` — ранний halt.

**Cache primer (опционально).** Claude API даёт ephemeral_1h-cache;
если N translator'ов одновременно стартуют одинаковый префикс промпта
(formatting_rules + glossary + ...), один из них **создаёт** кэш,
остальные **читают**. Эмпирически кэш шарится в Pro/Max. Флаг
`PARALLEL_CACHE_PRIMER=true` запускает 1 solo-statью первой, чтобы
гарантированно создать кэш, потом N-1 параллельно. По умолчанию
false — сразу N параллельно, потом по `cache_read_input_tokens` в
result_*.json видно, шарится кэш у тебя или нет.

## 4. Обход лимитов 5h / weekly

Anthropic-подписка имеет два уровня лимитов: 5-часовое session-окно
и недельный квота. При исчерпании translator-вызов возвращает
строку с «hit your limit … resets HH:MM» (session) или
«resets YYYY-MM-DDTHH:MM:SS» (weekly).

`corpus_tools/process_results.py` парсит эту строку, и orchestrator в
`_compute_next_wake_after_hit_limit` (`orchestrator.py:1503-1534`)
переводит её в `next_wake_at`:

- `HH:MM` → ближайшее это время + 5 минут (если уже прошло сегодня —
  завтра).
- `YYYY-MM-DDTHH:MM:SS` → как есть + 5 минут.
- иначе fallback `now + 1h05min`.

FSM уходит в `IDLE(idle_reason=hit_limit_wait)`, `_run_loop` спит до
`next_wake_at` через `asyncio.wait_for(_wake_event.wait(),
timeout=delta)`. По истечении — автоматически в PREPARING. Оператор
может прервать ожидание вручную через `/resume`.

Флаг `is_post_hit_limit_retry` защищает от петли «hit_limit → wake →
сразу опять hit_limit» (выход `post_hit_limit_loop` после второго
подряд).

## 5. Chunking + resume

### Chunking

Большие статьи (десятки тысяч символов исходника) не помещаются в
одно out-окно модели за разумное время и при сетевом обрыве теряют
весь output. Решение — **chunking на стороне translator-промпта**, не
orchestrator'а.

`templates/translation_prompt.md` Шаг 4 описывает алгоритм: накапливать
параграфы в чанк, пока сумма исходных символов не превысит
`{{chunk_budget_chars}}` (default 7500, конфиг
`CHUNK_BUDGET_CHARS`). При превышении — закрыть чанк (Write для
первого, Bash heredoc append для последующих) и начать новый.
Параграф длиннее бюджета не режется — становится своим чанком.

Тюнинг:
- меньше budget → больше чанков, меньше потерь при hit-limit, больше
  tool-overhead, риск дисторции связности;
- больше budget → меньше чанков, лучше связность, больше потерь при
  hit-limit.

### Resume

Когда translator упирается в hit_limit посреди статьи, частично
переведённый `.md` остаётся на диске. На следующем цикле:

1. `partial_state.inspect_partial` (corpus_tools) детектит partial-`.md`,
   парсит из него последний переведённый параграф K и заголовок.
2. `build_batch.render_resume_block` (corpus_tools) вшивает в промпт
   блок «вот файл NNN.md уже содержит параграфы start..K, продолжай
   с параграфа K+1; не дублируй уже сделанное».
3. Translator стартует с маркером `marker_rc=4` если завершился
   снова частично, или с `0` если дошёл до конца.

Orchestrator считает marker_rc=4 как **partial success** — статья не
идёт в completed (можно подобрать на следующем цикле), но и не идёт
в failed (translator работал корректно, просто упёрся в лимит).
Поле `partial_articles_in_run` сохраняется в `last_session.json` для
оператора.

## 6. gh_deploy: GitHub Pages push

`src/gh_deploy.py` деплоит собранный сайт в один primary `DeployTarget`:
`GH_REPO` (`<owner>/<repo>`), отдаётся через `https://<owner>.github.io/<repo>/`.

Pipeline (`deploy_site_to_pages`):
1. Сайт должен быть уже собран вызывающей стороной (`bot._run_build_site`).
2. Для primary таргета: копируем `Site/*` → `deploy_dir/`; всегда
   `.nojekyll`; `git add -A`; если diff пуст → skip; иначе commit + push.
3. **Token-in-URL push**: токен инжектится только в момент `git push`
   в URL `https://x-access-token:<TOKEN>@github.com/<owner>/<repo>.git`
   и **не записывается ни в `.git/config`, ни в commit metadata**. В
   логах токен заменяется на `***`.

Auto-deploy триггерится из bot.py на `article_done`-события с
дебаунсом (`GH_DEPLOY_DEBOUNCE_SECONDS`, default 30) и
минимальным интервалом между push'ами (`GH_DEPLOY_MIN_INTERVAL_SECONDS`,
default 600 = раз в 10 минут максимум). Manual `/push` обходит throttle.

## 7. Почему orchestrator, а не субагенты Desktop App

Это главный архитектурный вопрос: зачем огород с asyncio + subprocess,
если Claude Code и Claude Desktop App уже умеют спавнить субагентов?

Главная причина — **Desktop App не наследует** в спавнящихся
субагентов `effort` / thinking-mode родителя (по состоянию на май
2026 это поведение официально не задокументировано Anthropic; в CLI с
v2.1.133 наследование есть через `$CLAUDE_EFFORT`). Translator-субагенты,
запущенные изнутри Desktop App-оркестратора, стартовали **без extended
thinking**, что заметно роняло качество перевода (особенно на
сложных фрагментах Сулама, где надо разобрать многосоставную
ивритскую фразу и сохранить смысловую структуру).

zohar-translator обходит это, спавня translator'ов через **headless**
вызов `claude.exe -p ...` (subprocess): тогда загружаются settings и
CLAUDE.md пользователя, явно указывается модель, но не указывается
`effort` — получается adaptive thinking (на Opus 4.7 → xhigh для
сложных промптов).

Вторичные причины «orchestrator, а не Task-tool»:
- **State persistence**. FSM, current_run, events, last_session — всё
  на диске, переживает падение процесса и перезапуск. Task-tool —
  in-memory в рамках одного claude-session.
- **Параллелизм + контроль ресурсов**. Pool из N subprocess + явная
  отмена pending tasks на «жёстких» событиях. В Task-tool тяжело
  управлять размером пула и быстро отменять «волну» при hit-limit.
- **Real-time UI**. Bot читает events.jsonl и обновляет pinned
  dashboard в TG. Без отдельного процесса это сложно сделать.
- **GUI-обёртка**. `gui.pyw` (tkinter) — для людей, не имеющих
  привычки работать через CLI/IDE. Обёртка над `python main.py` как
  subprocess.

Источники для этого решения: Claude Code Changelog v2.1.133, GitHub
issue #25669, Claude API Effort docs, Claude Code CLI reference.

## 8. Точки расширения для другого корпуса

Под другой корпус меняется НЕ ядро, а:
- **Источник** — `reference/source_loader/download_sefaria.py` →
  свой загрузчик. Выход: `Source/<Work>/<Section>.json` с полем `he`.
- **План** — `<HEB_ROOT>/Source/articles_catalog.json` — что считать
  «главой», «статьёй». Формат: см.
  [`reference/source_loader/CATALOG_STRUCTURE.md`](reference/source_loader/CATALOG_STRUCTURE.md).
- **Промпт** — `templates/translation_prompt.md` под пары языков.
  Переменные `{{...}}` (см. `corpus_tools/build_batch.py:render_prompt`)
  сохранить.
- **Словарь** — переписать `glossary/glossary.json` под свою терминологию.
  Структура и `corpus_tools/glossary_tool.py` — переносимы.
- **Сайт** — `corpus_tools/build_site.py` под свою иерархию `book →
  chapter → article` и нужное оформление.

Все эти точки покрываются стадиями адаптации в `RUN_ME.md` (фаза C
плана).

## 9. Recovery-скрипты

В `corpus_tools/` три скрипта для штатного и аварийного восстановления
между запусками orchestrator'а. Они вызываются автоматически из самого
orchestrator'а (см. §2, переход IDLE → PREPARING), но при ручной
отладке их можно дёргать напрямую.

### 9.1 `partial_state.py`

Анализирует, в каком состоянии находится частично переведённый
`Translated/<book>/<chapter_ru>/NNN.md`. Возвращает один из четырёх
state'ов:

- `absent` — файла нет (или пустой).
- `partial` — параграфы идут сплошной последовательностью от `start`,
  но не доходят до `end`. `next_start = last + 1` — отсюда продолжить.
- `complete` — параграфы покрывают весь диапазон `[start..end]`,
  только `done.flag` отсутствует. Можно сразу проставить флаг.
- `corrupted` — нумерация параграфов сломана (пропуски, дубли,
  неправильный первый параграф, валидатор провалился, …). Файл нужно
  удалить и перевести заново.

Используется автоматически из `build_batch.py` (рендерит блок
`{{resume_block}}` для translator-промпта) и из
`mark_article_done.py` / `process_results.py` (решают, идёт ли
статья в `completed`/`failed` или остаётся pending для следующего
цикла).

Ручной CLI:

```
python corpus_tools/partial_state.py <md_path> <start> <end> \
    [--article-index N] [--no-validator]
```

### 9.2 `wake_recover.py`

Запускается в начале каждого нового цикла (wake-up) orchestrator'а.
Чинит два сценария, унаследованных от предыдущей сессии:

1. **`STALE_KILLED`** — предыдущая сессия оставила живым процесс
   `run_batch.sh` (плюс несколько `claude.exe -p`-детей). Новая сессия
   не ждёт их — `wake_recover.py` через PowerShell-фильтр находит и
   убивает stale-tree, чистит `.batch/run_batch.pid`,
   `.batch/run_batch.done`, `.batch/manifest.json`.
2. **`SELF_HEAL`** — `run_batch.sh` уже завершился (`.done` есть), но
   предыдущий orchestrator упал до того, как успел вызвать
   `process_results.py`. Тогда `wake_recover.py` сам прогоняет
   `process_results.py`, чтобы `report.json` отражал реальное
   состояние батча до того, как FSM продолжит цикл.

В нормальном случае печатает `OK` и ничего не делает. Ручной CLI:

```
python corpus_tools/wake_recover.py
```

### 9.3 `kill_stale_batch.py`

Узкий «гильотинный» инструмент: завершает живой `run_batch.sh` и его
`claude.exe -p`-детей по фильтру (имя процесса + содержимое
CommandLine). Идемпотентен — если ничего не нашёл, печатает «no stale
processes». В отличие от `wake_recover.py`, не чистит residue
`.batch/` и не делает self-heal — только убивает процессы.

Применять, если:
- visible orchestrator завис, а `run_batch.sh` в Task Manager видим;
- `wake_recover.py` отказался убирать (например, не нашёл по фильтру);
- нужно прибить старый цикл вручную перед запуском нового.

Ручной CLI:

```
python corpus_tools/kill_stale_batch.py
```
