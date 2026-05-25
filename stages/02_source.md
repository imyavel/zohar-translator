# Стадия 2 — Source loader

**Цель.** В `$HEB_ROOT/Source/` лежит исходный текст корпуса в формате,
понятном дальнейшему pipeline. Корпус скачан целиком (а не итеративно
во время перевода — это медленно и хрупко).

## 2.1 Развилка источника

**(Q 1 из 1: откуда грузим корпус?)**

- **(a) Sefaria.org** — fast-path. У нас есть готовый загрузчик
  [`reference/source_loader/download_sefaria.py`](../reference/source_loader/download_sefaria.py),
  работает через публичный Sefaria API. Подходит для любых текстов
  из их каталога (Танах, Мишна, Талмуд, Зоар, средневековые комментарии…).
  Лицензия большинства текстов CC BY или public domain — проверяй
  карточку текста на sefaria.org.
- **(b) Свой источник** — другой сайт, локальные файлы, API
  издательства, OCR-выгрузка. Нужно написать собственный loader
  по образцу `download_sefaria.py` (читай его как референс — там
  ~150 строк, видно, что делать).
- **(c) Файлы уже на руках** — корпус уже лежит локально в каком-то
  виде. Тогда задача — переформатировать его в структуру, которую
  ожидает pipeline (см. CATALOG_STRUCTURE.md).

## 2.2 Путь (a) — Sefaria fast-path

**(Q 1 из 1: какой текст с Sefaria переводим?)** Попроси оператора
дать URL страницы на sefaria.org с нужным текстом (например
`https://www.sefaria.org/Zohar`). Можешь предложить пройти на сайт и
скопировать ссылку.

Дальше определи `slug` для API (последний сегмент URL, обычно
совпадает с английским названием книги — `Zohar`, `Mishnah_Berakhot`,
`Bereishit`). Если непонятно — открой страницу через `WebFetch` и
найди в HTML канонический slug.

Установи переменные окружения и запусти загрузчик:

```
export SEFARIA_OUT="$HEB_ROOT/Source"
python reference/source_loader/download_sefaria.py <slug>
```

(Windows PowerShell: `$env:SEFARIA_OUT = "$env:HEB_ROOT\Source"; python reference\source_loader\download_sefaria.py <slug>`.)

**Это долгая сетевая операция.** Объясни оператору, что займёт от
минут (короткий трактат) до часов (полный Зоар). Спроси «начинаем?»
**одной кнопкой согласия**. После согласия — запусти и не отвлекай;
по завершении проверь количество скачанных файлов.

В конце в `$HEB_ROOT/Source/` появится:

- `<slug>.json` — основной текст постатейно (англ. + оригинал).
- `<slug>_<commentary>.json` — комментарии, если выбраны.
- `catalog.json` — оглавление со ссылками на статьи.

## 2.3 Путь (b) — свой loader

Прочитай оператора: какой формат источника (HTML, XML, plain text,
JSON API, PDF)? Какие поля нужны: оригинальный текст, перевод-подстрочник
(если есть), комментарии, метаданные (глава, статья, номер)?

Скопируй `download_sefaria.py` в новый файл, например
`reference/source_loader/<corpus>_loader.py`. Перепиши:

- Источник данных (HTTP / файлы / API).
- Парсинг (BeautifulSoup для HTML, lxml для XML, `json.load` для JSON).
- Маппинг полей источника → структуру `catalog.json` + `<slug>.json`
  (см. CATALOG_STRUCTURE.md).

Тестируй на одной главе — убедись, что результат читается
`articles_catalog.json`-генератором (он стоковый, его трогать не надо).

## 2.4 Путь (c) — файлы уже на руках

Спроси оператора, в каком они формате и где лежат. Дальше — то же,
что в (b): пишешь конвертер из их формата в наш Source-формат. Часто
проще: один python-скрипт, итерируешься по файлам, пишешь JSON.

## 2.5 Проверка результата

Независимо от пути:

```
ls "$HEB_ROOT/Source/"
```

Должно быть: непустой набор `.json` (или твоего формата) + `catalog.json`.
Открой `catalog.json` глазами, убедись, что иерархия глав/статей
читается. Если структуры нет — это норма: на стадии 3 ты соберёшь
`articles_catalog.json` (плоский список единиц перевода).

## 2.6 Запись прогресса

```json
{
  "current_stage": 3,
  "completed_stages": [1, 2],
  "answers": {
    ...,
    "stage2.source_kind": "sefaria_fast_path",
    "stage2.source_slug": "Zohar",
    "stage2.source_url": "https://www.sefaria.org/Zohar",
    "stage2.source_articles_count": 1287
  }
}
```

(`source_articles_count` — посчитай вручную через `len(catalog['articles'])`
или подобное, поможет оценить порядок длительности перевода.)

## Чек-лист стадии 2

- [ ] Источник выбран и зафиксирован в `answers`.
- [ ] `$HEB_ROOT/Source/` непуст, `catalog.json` есть.
- [ ] Оператор понимает, что качал, и согласен с объёмом.

Переходи к [`03_text_structure.md`](03_text_structure.md). Подробности
о структуре каталога см.
[`reference/source_loader/CATALOG_STRUCTURE.md`](../reference/source_loader/CATALOG_STRUCTURE.md).
