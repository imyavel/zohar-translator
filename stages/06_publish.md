# Стадия 6 — Publish target

**Цель.** Готов канал публикации перевода. После каждой закрытой
главы orchestrator коммитит результат, сайт обновляется. На стадии
8 (hand-off) оператор уже получит работающую ссылку.

## 6.1 Три варианта

**(Q 1 из 1: куда публикуем перевод?)**

- **(a) GitHub Pages по нашему шаблону.** Primary-репо с переводом.
  Авто-деплой через [`src/gh_deploy.py`](../src/gh_deploy.py) — он же
  коммитит, он же выкатывает. Минимум ручной работы.
- **(b) Свой таргет.** Другой хостинг, кастомный формат, отдельный
  CMS. Нужно править `gh_deploy.py` либо подменять `build_site.py`.
- **(c) Только локально.** Перевод копится в `$HEB_ROOT/Translated/`,
  сайт собирается локально через `build_site.py`, но никуда не
  заливается. Подходит для драфта или приватного перевода.

Дальше идём по выбранному пути; чужие пропускай.

## 6.2 Путь (a) — GitHub Pages

### 6.2.1 Параметры

Спроси по очереди (правило `(Q N из NN)`):

- **(Q 1 из 2: GitHub-аккаунт оператора?)** Например `your-username`. Если
  у оператора нет аккаунта — заведи: github.com/signup, пошагово,
  пароль и 2FA на стороне оператора.
- **(Q 2 из 2: имя primary-репо для перевода?)** Короткое, латиница,
  дефисы. Пример: `zohar-russian`, `mishna-translation`. Будет
  виден в URL `<owner>.github.io/<repo>/`.

Если оператору позже понадобится custom domain — он настраивает его
вручную через GitHub Settings → Pages → Custom domain; в `project.config`
ничего дополнительного не хранится.

### 6.2.2 GH_TOKEN

GitHub-токен — единственный секрет, который оператор добывает
сам, не ты. **Никогда не выводи токен в чат и не логируй.**

Веди оператора пошагово:

1. github.com → правый верхний угол → Settings.
2. Левое меню внизу → Developer settings.
3. Personal access tokens → **Fine-grained tokens** (не classic).
4. Generate new token. Expiration: 90 дней или дольше (оператору
   придётся обновлять).
5. Repository access: «Only select repositories», выбрать тот, что в
   Q2. Если репо ещё не создан — можно
   `All repositories`, потом перевыдашь под конкретный.
6. Permissions → Repository → **Contents: Read and write**, **Pages:
   Read and write**, **Metadata: Read-only** (обязательно).
7. Generate. Скопировать значение токена.

Попроси оператора **вставить** токен — приём через input, не через
echo. Запиши строкой `GH_TOKEN=<value>` **только в `.env`**.
`project.config` — публичный файл (его можно безопасно коммитить),
секреты туда класть нельзя.

### 6.2.3 Создание репо

Если репо ещё не существует — создай через GitHub REST API:

```
curl -X POST -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d '{"name":"<repo>","private":false,"description":"...","auto_init":true}'
```

(Windows PowerShell: `Invoke-RestMethod -Method POST ...` — тот же
endpoint.) Если 422 «name already exists» — репо уже есть, ок.

Затем включи GitHub Pages: для модернового layout достаточно
запушить ветку `gh-pages` (или `main` + папку `/docs`). `gh_deploy.py`
делает это автоматически при первом успешном переводе главы.

### 6.2.4 Параметры в project.config (публично) и .env (секрет)

`project.config` (можно коммитить):

```
GH_REPO=<owner>/<repo>
GH_COMMIT_USER=zohar-translator-bot
GH_COMMIT_EMAIL=zohar-translator-bot@users.noreply.github.com
```

`.env` (никогда не коммитить — он в `.gitignore`):

```
GH_TOKEN=<...секрет, никогда не лоудить в чат...>
```

### 6.2.5 Лэндинг

Многоязычный лэндинг — отдельная задача, делается после прохождения
stages. Не блокирует стадию 6.

## 6.3 Путь (b) — свой таргет

Прочитай оператора: куда публиковать (S3? GitLab Pages? собственный
nginx? Notion? Telegram-канал?). Дальше — два места правки:

- `corpus_tools/build_site.py` — генерация HTML/MD/whatever из
  переведённых статей. По умолчанию даёт статический сайт; адаптируй
  формат.
- `src/gh_deploy.py` — собственно деплой. Замени git-push на вызов
  своего канала (rsync, scp, API).

Сохрани интерфейсный контракт: деплой триггерится из `src/bot.py` —
`_schedule_gh_deploy` → `_run_gh_deploy` →
`gh_deploy.deploy_site_to_pages(heb_root, targets, token, …)`. Эту
сигнатуру при адаптации под чужой канал менять не нужно — поменяй
тело `deploy_site_to_pages` (или подмени модуль целиком).

## 6.4 Путь (c) — только локально

Удали `GH_*` ключи из `project.config` (или оставь пустыми).
`config.py` обнаружит отсутствие и отключит `gh_deploy.py`.
`build_site.py` всё равно соберёт сайт локально в
`$HEB_ROOT/Translated/Site/`, оператор сможет открыть `index.html` в
браузере.

## 6.5 Запись прогресса

```json
{
  "current_stage": 7,
  "completed_stages": [1, 2, 3, 4, 5, 6],
  "answers": {
    ...,
    "stage6.publish_target": "github_pages",
    "stage6.repo": "<owner>/<repo>",
    "stage6.gh_token_set": true,
    "stage6.landing_deferred": true
  }
}
```

`gh_token_set: true` — флаг «токен есть в `.env`/`project.config`»,
само значение не дублируй в `progress.json`.

## Чек-лист стадии 6

- [ ] Target выбран и зафиксирован.
- [ ] Если (a): репо создан, `GH_TOKEN` **в `.env`** (не в
      `project.config`), `GH_REPO`/`GH_COMMIT_USER`/`GH_COMMIT_EMAIL`
      в `project.config`.
- [ ] Если (b): `build_site.py` и `gh_deploy.py` адаптированы и
      проверены.
- [ ] Если (c): `GH_*` обнулены, локальная сборка протестирована.
- [ ] **`progress.json` и `project.config` не содержат значения
      токена** — только флаг `gh_token_set: true`.

Переходи к [`07_smoke.md`](07_smoke.md).
