# API v0.1 — контракт для фронтенда

Базовый URL для фронтенда: `/api` (Vite проксирует его на `http://127.0.0.1:8000`); прикладные маршруты начинаются с `/api/v1`. JSON в UTF-8. Автогенерируемая спецификация: `/api/openapi.json`, Swagger UI: `/api/docs`. Этот файл обновлять при каждом изменении маршрутов, статусов или полей. Даты — ISO 8601 UTC. Идентификаторы — UUID в строковом виде. `GET` возвращает `200`, создание — `201`; ошибки: стандартное FastAPI `{"detail": ...}` (`404`, `409`, `422`, `503`). Авторизации в прототипе нет: не выставлять публично.

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET | `/api/health` и `/api/health/live` | Жив процесс, `{"status":"ok"}` |
| GET | `/api/health/ready` | Доступна БД; при сбое `503` |
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
3. Показывать `result.normalized_attributes`, `result.issues`, `result.candidates`, `result.next_steps`. Автоматически принимать код только при `status=resolved` и непустом `selected_ktru_code`.
4. Подтверждение/исправление отправлять в `/api/v1/analyses/{analysis_id}/feedback`. История оценок сохраняется, пока не влияет на базовый алгоритм.

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

Ответ (`id`, `created_at` условные):

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
    "selected_ktru_code": "DEMO-CUP-PAPER"
  },
  "engine_version": "catalog-baseline-v1",
  "created_at": "2026-10-09T09:00:00Z"
}
```

Коды `status`: `needs_data` (пустой справочник), `needs_input` (недостаточно информации либо нет совместимых позиций), `ambiguous` (несколько вариантов), `resolved` (ровно один конечный код, заполнены обязательные поля, нет ошибок). В `issues[].code`: `catalog_empty`, `unknown_attribute`, `duplicate_attribute`, `unit_mismatch`, `invalid_number`, `no_match`. `field` — путь к ошибочному полю запроса. `candidates` ограничены 20 вариантами; не используйте список как полную выгрузку КТРУ.

`GET /api/v1/catalog/categories` возвращает `id`, `parent_id`, `ktru_code`, `name`, `is_leaf`. Маршрут `/attributes` возвращает `id`, `name`, `value_type` (`text`/`number`/`boolean` в данных), `unit`, `aliases`, `required`, `allowed_values`. Пока базовый алгоритм проверяет числовой тип, совпадение единицы и перечислимые значения; `boolean` обрабатывает как текст. Перевод единиц, поиск опечаток, распознавание текста и ранжирование названия ожидают модельный модуль. До этого не показывать результат как ML прогноз.

Создание СТЕ: `{"source":"organizer","external_id":"123","title":"...","description":null,"category_id":null,"raw_payload":{}}`. `source + external_id` должны быть уникальны. `raw_payload` нужен для трассировки исходной строки, основной API не предполагает его структуру.

Feedback: `{"verdict":"corrected","corrected_ktru_code":"...","corrected_attributes":[{"name":"Материал","value":"бумага","unit":null}],"comment":"..."}`. `verdict` — `accepted`, `rejected` или `corrected`; при `corrected` нужен код либо характеристика. Ответ повторяет поля и добавляет `id`, `analysis_id`, `created_at`.

## Совместимость

Поля и статусы выше — контракт v0.1. Изменения сначала в Pydantic схемах/OpenAPI и этом документе в одном коммите. Алгоритмический модуль должен сохранять эти внешние формы. Для больших наборов данных добавить полнотекстовый поиск и постраничную навигацию с `total` в следующей версии, не менять молча тип ответа.
