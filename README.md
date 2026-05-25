# zohar-translator

Универсальный orchestrator для длинного LLM-перевода больших корпусов.

Реальный референс — наш собственный перевод комментария «Перуш ха-Сулам»
на книгу Зоар: <https://imyavel.github.io/zohar-sulam/> (CC BY 4.0).

## Что внутри

- **`src/`** — ядро: orchestrator с FSM (`IDLE → PREPARING → RUNNING →
  FINALIZING`), пул параллельных translator-subprocess'ов, обход
  лимитов Claude Pro/Max (5h-session + weekly), resume через partial
  state, Telegram-бот, GUI (tkinter), деплоер GitHub Pages.
- **`corpus_tools/`** — CLI-скрипты, обслуживающие конкретный корпус:
  планировщик батчей, словарь, валидаторы, генератор статичного сайта,
  утилиты восстановления.
- **`glossary/glossary.json`** — наш эталонный словарь (>300 правил,
  иврит/арамит → русский). Для своего корпуса оператор берёт у нас
  **структуру и методологию использования через `glossary_tool.py`**,
  не содержимое.
- **`templates/`** — главный промпт переводчика, правила оформления,
  гайд по словарю.
- **`reference/source_loader/`** — `download_sefaria.py` (как пример
  загрузчика корпуса) и [`CATALOG_STRUCTURE.md`](reference/source_loader/CATALOG_STRUCTURE.md)
  (что такое `catalog.json` / `articles_catalog.json`).

## Развёртывание

Главная точка входа — **`RUN_ME.md`** (будет добавлен в фазе C). Это
driver для LLM-агента (Claude Code или аналога), который проводит
оператора через 8 стадий адаптации: окружение, источник, структура
текста, словарь, промпт, target публикации, smoke-run, hand-off.

Единственный prerequisite — установленный Claude Code (или иной
LLM-агент) и действующая подписка.

## Способы запуска (после развёртывания)

Все три исполняются из корня репо.

```bash
# 1. GUI (tkinter, без зависимостей кроме stdlib)
python src/gui.pyw

# 2. Orchestrator + Telegram-бот headless
python src/main.py

# 3. Orchestrator headless без Telegram
python src/main.py --no-bot
```

Полный список флагов: `python src/main.py --help`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
