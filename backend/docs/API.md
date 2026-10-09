# API v0.1 — контракт для фронтенда

Базовый URL для фронтенда: `/api` (Vite проксирует его на `http://127.0.0.1:8000`); прикладные маршруты начинаются с `/api/v1`. JSON в UTF-8. Автогенерируемая спецификация: `/api/openapi.json`, Swagger UI: `/api/docs`. Этот файл обновлять при каждом изменении маршрутов, статусов или полей. Даты — ISO 8601 UTC. Идентификаторы — UUID в строковом виде. `GET` возвращает `200`, создание — `201`; ошибки: стандартное FastAPI `{"detail": ...}` (`404`, `409`, `422`, `503`). Авторизации в прототипе нет: не выставлять публично.

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET | `/api/health` и `/api/health/live` | Жив процесс, `{"status":"ok"}` |
| GET | `/api/health/ready` | Доступна БД и таблицы приложения после миграции; `{"status":"ok"}`; при сбое или отсутствии таблиц `503` |
| GET | `/api/v1/catalog/status` | `{"categories":0,"attributes":0,"ste":0}`; фронт показывает состояние отсутствия данных |
| GET | `/api/v1/catalog/categories?parent_id=&q=&limit=50&offset=0` | Список категорий/позиций КТРУ; `limit` 1–100; без `parent_id` возвращает все уровни |
| GET | `/api/v1/catalog/categories/{category_id}/attributes` | Определения характеристик и правила выбранного кода; `404` |
| GET | `/api/v1/ste?q=&limit=50&offset=0` | Карточки СТЕ; пустой список без данных |
| POST | `/api/v1/ste` | Создать карточку; дубль `(source,external_id)` → `409` |
| GET | `/api/v1/ste/{ste_id}` | Карточка или `404` |
| POST | `/api/v1/analyses` | Создать синхронный анализ и сохранить результат |
| GET | `/api/v1/analyses/{analysis_id}` | Получить сохранённый результат или `404` |
| POST | `/api/v1/analyses/{analysis_id}/feedback` | Сохранить оценку/исправление или `404` |

## Основной сценарий

1. `GET /api/v1/catalog/status`. Если `categories=0`, показывать ожидание датасета; анализ всё равно вернёт `needs_data`.
2. Пользователь вводит название, описание, пары `name`/`value`/`unit`. После каждого уточнения отправлять новый `POST /api/v1/analyses` (предыдущие результаты сохраняются; это не обновление).
3. Показывать `result.normalized_attributes`, `result.issues`, `result.candidates`, `result.next_steps`. Даже единственный кандидат без ручного подтверждения возвращается с `needs_input` и `selected_ktru_code=null`. Название и описание не участвуют в подборе: пользователь должен проверить соответствие категории товару.
4. После явного выбора пользователя повторить `POST /api/v1/analyses` с текущими характеристиками и `confirmed_ktru_code`. Принять выбранный код только при `status=resolved`, непустом `selected_ktru_code` и `result.confirmed_by_user=true`. Это новый анализ; предыдущий результат не меняется.
5. Оценку/исправление результата отправлять в `/api/v1/analyses/{analysis_id}/feedback`. Feedback не подтверждает код, не меняет статус анализа и пока не влияет на алгоритм.

Запрос:

```json
{
  "title": "Стакан 200 мл",
  "description": "Для напитков",
  "attributes": [
    {"name": "Объем", "value": "200", "unit": "мл"},
    {"name": "Материал", "value": "бумага", "unit": null}
  ]
}
```

Вместо `title` можно передать `ste_id` существующей карточки. Если переданы оба, явный `title` имеет приоритет; `description` наследуется от СТЕ, если не задан. Минимум одно из `title` и `ste_id` обязательно. Максимум 100 входных характеристик.

Первый ответ на запрос выше: `status=needs_input`; `result.candidates` содержит `DEMO-CUP-PAPER`, `result.selected_ktru_code=null`, `result.confirmed_by_user=false`, `result.next_steps` предлагает проверить товар и передать `confirmed_ktru_code`.

Необязательный `confirmed_ktru_code` — строка длиной 1–64 после удаления пробелов по краям; отсутствие поля или `null` означает отсутствие подтверждения. Пустая строка даёт `422`. Код сравнивается точно, с учётом регистра. Проверка всегда выполняется заново по текущему каталогу и текущим характеристикам. При нескольких совместимых кандидатах ручной выбор одного разрешён; обязательные характеристики проверяются именно для выбранного кандидата. Несовместимый, неизвестный или промежуточный код даёт `422`, анализ не сохраняется:

```json
{"detail":{"code":"incompatible_ktru_code","field":"confirmed_ktru_code","message":"Подтверждённый код не входит в совместимые конечные позиции КТРУ"}}
```

Если код совместим, но есть ошибки входа или отсутствуют обязательные характеристики, ответ `201` со статусом `needs_input`, `selected_ktru_code=null`, `confirmed_by_user=false`; исправьте характеристики и повторите запрос с подтверждением. При пустом каталоге запрос без подтверждения даёт `needs_data`, с кодом подтверждения — `422`, поскольку совместимых кандидатов нет.

Запрос после ручного подтверждения:

```json
{
  "title": "Стакан 200 мл",
  "description": "Для напитков",
  "attributes": [
    {"name": "Объем", "value": "200", "unit": "мл"},
    {"name": "Материал", "value": "бумага", "unit": null}
  ],
  "confirmed_ktru_code": "DEMO-CUP-PAPER"
}
```

Ответ после подтверждения (`id`, `created_at` условные):

```json
{
  "id": "e82ab288-3ce6-44f7-b6fb-393b1e2d8069",
  "ste_id": null,
  "title": "Стакан 200 мл",
  "description": "Для напитков",
  "input_attributes": [
    {"name": "Объем", "value": "200", "unit": "мл"},
    {"name": "Материал", "value": "бумага", "unit": null}
  ],
  "status": "resolved",
  "result": {
    "normalized_attributes": [
      {"attribute_id": "...", "name": "Объем", "value": "200", "unit": "мл", "source": "input"},
      {"attribute_id": "...", "name": "Материал", "value": "бумага", "unit": null, "source": "input"}
    ],
    "candidates": [
      {"category_id": "...", "ktru_code": "DEMO-CUP-PAPER", "name": "Демонстрация: стакан бумажный", "matched_attributes": ["Объем", "Материал"], "missing_required": []}
    ],
    "issues": [],
    "next_steps": [],
    "selected_ktru_code": "DEMO-CUP-PAPER",
    "confirmed_by_user": true
  },
  "engine_version": "catalog-baseline-v2",
  "created_at": "2026-10-09T09:00:00Z"
}
```

Пример вызова из Vite; `code` передаётся обработчиком явного выбора пользователя, а не выбирается автоматически из первого ответа:

```js
async function analyze(payload, code = null) {
  const response = await fetch('/api/v1/analyses', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payload, confirmed_ktru_code: code }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(JSON.stringify(body.detail));
  return body;
}
// await analyze(payload) — первичный анализ.
// await analyze(payload, chosenCode) — после ручного подтверждения.
```

Коды `status`: `needs_data` (пустой справочник без подтверждения), `needs_input` (ожидается ручное подтверждение единственного кандидата, недостаёт обязательных характеристик, есть ошибки входа либо нет совместимых позиций), `ambiguous` (несколько вариантов без подтверждения и ошибок входа), `resolved` (пользователь подтвердил совместимый конечный код, его обязательные поля заполнены, ошибок входа нет). В `issues[].code`: `catalog_empty`, `unknown_attribute`, `duplicate_attribute`, `unit_mismatch`, `invalid_number`, `no_match`. `field` — путь к ошибочному полю запроса. `candidates` ограничены 20 вариантами; подтверждение проверяется по полному набору кандидатов, выбранный кандидат обязательно входит в ответ. Не используйте список как полную выгрузку КТРУ.

`result.confirmed_by_user` — булево поле, `true` только при успешном ручном подтверждении. Оно обозначает явный выбор через запрос, а не независимую проверку текста, авторства или правильности кода. У новых анализов `engine_version=catalog-baseline-v2`. Сохранённые ранее результаты не пересчитываются: старый `resolved` может иметь `confirmed_by_user=false`; фронт должен требовать ручное подтверждение новым запросом.

В `attributes` начальные и конечные пробелы `name`, `value`, `unit` удаляются; пустые после этого `name` и `value` дают `422`. Отсутствующая единица наследуется из справочника. Явно переданная единица должна совпадать с единицей справочника, включая отсутствие единицы у безразмерной характеристики; иначе `unit_mismatch` и `needs_input`, без преобразования. Числовые перечислимые значения сравниваются как числа: `200`, `200.00` и `200,0` эквивалентны.

`GET /api/v1/catalog/categories` возвращает `id`, `parent_id`, `ktru_code`, `name`, `is_leaf`. Маршрут `/attributes` возвращает `id`, `name`, `value_type` (`text`/`number`/`boolean` в данных), `unit`, `aliases`, `required`, `allowed_values`. Пока базовый алгоритм проверяет числовой тип, совпадение единицы и перечислимые значения; `boolean` обрабатывает как текст. Перевод единиц, поиск опечаток, распознавание текста и ранжирование названия ожидают модельный модуль. До этого не показывать результат как ML прогноз.

Создание СТЕ: `{"source":"organizer","external_id":"123","title":"...","description":null,"category_id":null,"raw_payload":{}}`. `source + external_id` должны быть уникальны. `raw_payload` нужен для трассировки исходной строки, основной API не предполагает его структуру.

Feedback: `{"verdict":"corrected","corrected_ktru_code":"...","corrected_attributes":[{"name":"Материал","value":"бумага","unit":null}],"comment":"..."}`. `verdict` — `accepted`, `rejected` или `corrected`; при `corrected` нужен код либо характеристика. Ответ повторяет поля и добавляет `id`, `analysis_id`, `created_at`.

## Совместимость

Поля и статусы выше — контракт v0.1. Изменения сначала в Pydantic схемах/OpenAPI и этом документе в одном коммите. Алгоритмический модуль должен сохранять эти внешние формы. Для больших наборов данных добавить полнотекстовый поиск и постраничную навигацию с `total` в следующей версии, не менять молча тип ответа.
