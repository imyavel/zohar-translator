# Стадия 6 — Publish target

**Цель.** Готов канал публикации перевода. После каждой закрытой
главы orchestrator коммитит результат, сайт обновляется. На стадии
8 (hand-off) оператор уже получит работающую ссылку.

## 6.1 Три варианта

**(Q 1 из 1: куда публикуем перевод?)**

- **(a) GitHub Pages по нашему шаблону.** Primary-репо с переводом +
  опциональное зеркало + опц. custom domain. Авто-деплой через
  [`src/gh_deploy.py`](../src/gh_deploy.py) — он же коммитит, он же
  выкатывает. Минимум ручной работы.
- **(b) Свой таргет.** Другой хостинг, кастомный формат, отдельный
  CMS. Нужно править `gh_deploy.py` либо подменять `build_site.py`.
- **(c) Только локально.** Перевод копится в `$HEB_ROOT/Translated/`,
  сайт собирается локально через `build_site.py`, но никуда не
  заливается. Подходит для драфта или приватного перевода.

Дальше идём по выбранному пути; чужие пропускай.

## 6.2 Путь (a) — GitHub Pages

### 6.2.1 Параметры

Спроси по очереди (правило `(Q N из NN)`):

- **(Q 1 из 4: GitHub-аккаунт оператора?)** Например `imyavel`. Если
  у оператора нет аккаунта — заведи: github.com/signup, пошагово,
  пароль и 2FA на стороне оператора.
- **(Q 2 из 4: имя primary-репо для перевода?)** Короткое, латиница,
  дефисы. Пример: `zohar-russian`, `mishna-translation`. Будет
  виден в URL `<owner>.github.io/<repo>/`.
- **(Q 3 из 4: нужно зеркало?)** Иногда полезно: один репо публичный
  (CC BY), второй приватный с метаданными или draft'ами. Если
  «нет» — пропусти.
- **(Q 4 из 4: custom domain?)** Например `zohar-sulam.example.com`.
  Если нет домена — пропусти, останется
  `<owner>.github.io/<repo>/`. Если есть — оператор отдельно
  настраивает DNS CNAME → `<owner>.github.io`.

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
   Q2 (и зеркало из Q3, если есть). Если репо ещё не создан — можно
   `All repositories`, потом перевыдашь под конкретный.
6. Permissions → Repository → **Contents: Read and write**, **Pages:
   Read and write**, **Metadata: Read-only** (обязательно).
7. Generate. Скопировать значение токена.

Попроси оператора **вставить** токен — приём через input, не через
echo. Запиши в `project.config` строкой `GH_TOKEN=<value>` (или в
`.env`, если ты используешь его как основной — оба варианта
поддерживаются `src/config.py`).

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

### 6.2.4 Параметры в project.config

```
GH_TOKEN=<...секрет, никогда не лоудить в чат...>
GH_REPO=<owner>/<repo>
GH_MIRROR_REPO=<owner>/<mirror>     # опц.
GH_CUSTOM_DOMAIN=<domain>            # опц.
GH_COMMIT_USER=zohar-translator-bot
GH_COMMIT_EMAIL=zohar-translator-bot@users.noreply.github.com
```

### 6.2.5 Лэндинг на 11 локалях

Отдельная подзадача — `docs/index.html` с переключателем 11 языков и
auto-detect по IP. Подробности — в Plan.md разработчика
(блок B4). Эту задачу можно сделать **после** того, как pipeline
заработал и есть содержательный сайт-перевод. Не блокируй на ней
стадию 6.

## 6.3 Путь (b) — свой таргет

Прочитай оператора: куда публиковать (S3? GitLab Pages? собственный
nginx? Notion? Telegram-канал?). Дальше — два места правки:

- `corpus_tools/build_site.py` — генерация HTML/MD/whatever из
  переведённых статей. По умолчанию даёт статический сайт; адаптируй
  формат.
- `src/gh_deploy.py` — собственно деплой. Замени git-push на вызов
  своего канала (rsync, scp, API).

Сохрани интерфейсный контракт: `gh_deploy.py` вызывается из
`src/orchestrator.py` после завершения главы (см. там
`_maybe_deploy_chapter`), сигнатура одного метода — менять не нужно.

## 6.4 Путь (c) — только локально

Удали `GH_*` ключи из `project.config` (или оставь пустыми).
`config.py` обнаружит отсутствие и отключит `gh_deploy.py`.
`build_site.py` всё равно соберёт сайт локально в
`$HEB_ROOT/site/`, оператор сможет открыть `index.html` в браузере.

## 6.5 Запись прогресса

```json
{
  "current_stage": 7,
  "completed_stages": [1, 2, 3, 4, 5, 6],
  "answers": {
    ...,
    "stage6.publish_target": "github_pages",
    "stage6.repo": "<owner>/<repo>",
    "stage6.mirror": null,
    "stage6.custom_domain": null,
    "stage6.gh_token_set": true,
    "stage6.landing_deferred": true
  }
}
```

`gh_token_set: true` — флаг «токен есть в `.env`/`project.config`»,
само значение не дублируй в `progress.json`.

## Чек-лист стадии 6

- [ ] Target выбран и зафиксирован.
- [ ] Если (a): репо создан, токен в `.env`, переменные в
      `project.config`.
- [ ] Если (b): `build_site.py` и `gh_deploy.py` адаптированы и
      проверены.
- [ ] Если (c): `GH_*` обнулены, локальная сборка протестирована.
- [ ] **`progress.json` и `project.config` не содержат значения
      токена** — только флаг `gh_token_set: true`.

Переходи к [`07_smoke.md`](07_smoke.md).
