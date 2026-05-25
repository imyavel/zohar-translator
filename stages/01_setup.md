# Стадия 1 — Environment

**Цель.** Python ≥ 3.11 на машине, зависимости установлены, скелет
прогресс-файлов создан, стоковый `gui.pyw` стартует и закрывается без
ошибок.

## 1.1 Проверка Python

Прочитай `python --version` (Windows может быть `py --version`).

- Версия ≥ 3.11 → переходи к 1.2.
- Версия < 3.11 или нет вовсе → установи:
  - **Windows:** `winget install Python.Python.3.12` (или попроси
    оператора скачать с python.org, если winget недоступен).
  - **macOS:** `brew install python@3.12`.
  - **Linux (Debian/Ubuntu):** `sudo apt install python3.12 python3.12-venv`.

После установки убедись, что `python` (или `py`) теперь указывает на
новую версию.

## 1.2 Клонирование репо

Если оператор уже находится внутри клонированного `zohar-translator/` —
пропусти. Иначе:

```
git clone https://github.com/<owner>/zohar-translator.git
cd zohar-translator
```

Перейди в корень репо — все дальнейшие команды выполняются оттуда.

## 1.3 Виртуальное окружение

Создай и активируй venv:

- **Windows (PowerShell):** `python -m venv .venv; .\.venv\Scripts\Activate.ps1`
- **macOS/Linux:** `python -m venv .venv && source .venv/bin/activate`

Затем установи зависимости:

```
pip install -r requirements.txt
```

Это: `python-telegram-bot >=21,<22`, `python-dotenv`, `pydantic`,
`requests`. Никакой нативщины — всё ставится за минуту.

## 1.4 Smoke-import

Проверь, что ядро импортируется без ошибок:

```
python -c "from src import config, state, orchestrator, gh_deploy, bot; print('OK')"
```

Если упало — читай traceback, обычно это либо несоответствие версии
Python (нужен 3.11+), либо забытая зависимость. Покажи оператору
ошибку, исправь.

## 1.5 Шаблоны прогресса

Создай два файла в корне репо из шаблонов:

```
cp progress.json.template progress.json
cp project.config.template project.config
```

(Windows: `Copy-Item progress.json.template progress.json`.) Эти файлы
**в `.gitignore`** — они локальны для оператора, в репо не попадают.

## 1.6 Базовые параметры корпуса

Тебе нужно от оператора два значения, чтобы заполнить `project.config`:

**(Q 1 из 2: где будет жить корпус?)** Это путь к рабочей папке, куда
скачается исходный текст, лягут переводы, кэш батчей. По умолчанию —
`~/zohar-corpus` (Linux/Mac) или `C:\Users\<имя>\zohar-corpus` (Win).
Можно где угодно — главное, чтобы было свободно несколько ГБ и путь
без пробелов и кириллицы.

Получив ответ — создай папку (`mkdir -p` / `New-Item -ItemType Directory`)
и запиши `HEB_ROOT=<путь>` в `project.config`.

**(Q 2 из 2: короткое имя корпуса для логов?)** Например `zohar`,
`mishna`, `genesis-rus`. Только латиница, цифры, дефис/подчёркивание.
Используется в названии TG-чата, заголовках сайта, именах временных
файлов. Запиши в `project.config` как `CORPUS_NAME=<имя>`.

## 1.7 Smoke-запуск GUI

Запусти GUI один раз, чтобы убедиться, что tkinter стартует и окно
открывается:

```
python src/gui.pyw
```

Окно должно появиться (пустое, без активных батчей — нормально). Если
окно появилось — попроси оператора его закрыть; на этой стадии GUI
больше не нужен. Если окно не появилось / упало — обычно проблема в
tkinter (на Linux может потребоваться `sudo apt install python3-tk`).

## 1.8 Запись прогресса

Обнови `progress.json`:

```json
{
  "current_stage": 2,
  "completed_stages": [1],
  "answers": {
    "stage1.python_version": "3.12.4",
    "stage1.heb_root": "/home/user/zohar-corpus",
    "stage1.corpus_name": "zohar"
  },
  "notes": []
}
```

Конкретные значения подставь из ответов оператора.

## Чек-лист стадии 1

- [ ] `python --version` ≥ 3.11
- [ ] `.venv` создан и активирован
- [ ] `pip install -r requirements.txt` без ошибок
- [ ] Smoke-import `from src import ...` без ошибок
- [ ] `progress.json` и `project.config` созданы
- [ ] `HEB_ROOT` и `CORPUS_NAME` известны и записаны
- [ ] `gui.pyw` хотя бы раз открылся и закрылся

Когда всё отмечено — переходи к [`02_source.md`](02_source.md).
