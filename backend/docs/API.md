# API v0.2 — stateless контракт frontend

Базовый URL `/api`. Vite и Docker nginx сохраняют префикс. OpenAPI: `/api/openapi.json`, Swagger: `/api/docs`. Сервер не хранит анализы: каждый POST передаёт полное текущее состояние, ответ полностью заменяет предыдущий результат.

## Маршруты

| Метод | Путь | Результат |
|---|---|---|
| GET | `/api/health`, `/api/health/live` | 200 `{"status":"ok"}` — процесс работает |
| GET | `/api/health/ready` | 200 status/catalog_version/engine_version либо 503 `detail.code` и catalog_version |
| GET | `/api/v1/catalog/status` | 200 categories/positions/catalog_version/ready/reason |
| POST | `/api/v1/analyses` | 200 полный результат; 422 неверный ввод/подтверждение; 429 заняты слоты; 503 внутренняя ошибка |

Старые серверные GET сохранённого анализа, CRUD СТЕ, сохраняемый feedback и маршруты чтения категорий/правил удалены (404). Их заменяет полный ответ POST; analysis_id, ste_id, title, raw_payload и старый контейнер result не используются. Frontend вызывает только POST анализа. Миграция не требуется: серверного хранилища нет.

## Запрос

```json
{
  "description": "Дрель электрическая; мощность: 650 Вт",
  "attributes": [],
  "additional": ["Для мастерской"],
  "confirmed_category_id": null,
  "answers": [],
  "confirmed_ktru_code": null,
  "candidate_limit": 20
}
```

description: непустой исходный текст до 12000 символов. attributes: до 100 объектов `{id, name, value, unit?, unknown?, subject?, replaces?}`. id уникален, до 128; name до 256, value до 512, unit до 64. subject: `product` (по умолчанию), `packaging`, `unspecified`. unknown по умолчанию false. `replaces` — идентификатор предыдущего извлечённого/ответного факта либо `fragment:start:end`, если пользователь явно исправляет/размечает фрагмент. Неприменимый идентификатор отклоняется: исправление не должно незаметно потеряться после смены текста. Ответ сохраняет original_facts и corrections; facts содержит действующее состояние.

Скобочные интервалы извлекаются вместе с явно указанной единицей: `Стол; Высота: [70; 80] см` → value=`[70;80]`, unit=`см`; raw/span сохраняют исходный фрагмент без изменений. Поддерживаются `[a;b]`, `(a;b)`, `[a;b)`, `(a;b]`, знаки минус и десятичная запятая. Пустые интервалы с равными границами и хотя бы одной открытой стороной не допускаются: проверка даёт incompatible/invalid_number, selected_ktru_code=null; подтверждение такого исключённого кода — 422. Они не нормализуются в точку. `[750;750]` — допустимая точка. Аналогичные пустые ограничения каталога отклоняются при загрузке (readiness 503 resources_invalid).

additional: до 100 строк по 1024 символа. answers: до 100 объектов `{question_id, value?, unit?, unknown?}`, один текущий ответ на идентификатор. «Не знаю»: value=null, unknown=true. confirmed_category_id: до 128 символов, confirmed_ktru_code: до 64. candidate_limit: 1–100, по умолчанию 20; ограничивает выдачу, а не проверяемую область. Все остальные поля запрещены, включая клиентские verification/normalized_value: доказательства вычисляются заново.

Ответы предыдущей категории сохраняются как источник явной правки через replaces; в новой категории они не становятся действующими фактами или доказательством. Идентификатор вопроса сверяется со snapshot каталога.

При исправлении фактов сбрасывайте confirmed_ktru_code. При смене исходного текста сбрасывайте категорию, ответы и устаревшие replaces; при смене категории удаляйте её ответ и подтверждение кода. Свойства другой категории сохраняются как дополнительные сведения, но не считаются доказательством.

## Ответ

Всегда: status, original_text, normalized_text, original_facts, facts, unparsed, additional, categories, candidates, excluded, candidate_count, excluded_count, scope, issues, corrections, next_question, next_steps, verification, selected_ktru_code, catalog_version, engine_version.

- Fact: id/name/value/unit/raw/span/source/subject/negated/unknown/normalized_value/normalized_unit/mappings. source: text/input/answer. span — полуоткрытый диапазон символов исходного текста либо null. Нормализация не меняет исходный факт.
- Fragment: id/raw/span. Нераспознанные и дополнительные сведения не теряются.
- CategoryCandidate: id/name/rank/rank_score/reasons. rank_score — RRF, не вероятность. Предложение требует подтверждения типа пользователем.
- Candidate: ktru_code/name/okpd2/outcome/checks/missing_required/has_characteristics. outcome: compatible/incompatible/unknown. Check: property_id/name/outcome/reason/fact_ids/value_codes. Шаблон не становится конечным кандидатом. Допустимые элементы сохраняют свои единицы и value_code в каталоге.
- candidate_count/excluded_count — полные количества до лимита ответа. scope: category_id/position_count/evaluated_count/complete. Весь конечный пул выбранной области проверяется до ограничения UI.
- Issue: code/severity/explanation/fact_id/property_id/candidate_code/rule_source/suggested_action/confirmation_required. severity: info/warning/error. Correction: fact_id/kind/original/normalized/explanation/confirmation_required; kind: auto_normalization/proposed_text_fix/proposed_fact_change. Безопасное преобразование единицы показывается; изменение факта требует пользовательского ввода.
- next_question: null либо id/kind/title/reason/property_name/options/unit/allow_unknown; kind: conflict/category/property/code; options: value/label. По одному вопросу. Неизвестный ответ не подтверждает значение, уже отвеченный вопрос не повторяется бесконечно.

Варианты вопроса свойства объединяются из проверенных правил всех оставшихся позиций той же семантики и объекта (товар/упаковка); правила разных объектов не смешиваются.
- verification: type_verified/ktru_unique/fields_complete/value_verified/catalog_scope_complete/value_indistinguishable/confirmed_by_user/blocking_issues — boolean. confirmed_by_user отражает присланное явное подтверждение кода, а не успешность проверки.

## Статусы и подтверждение

`needs_data`: нет пригодного каталога/индекса/модели или недостаточно подтверждённых ограничений/покрытия. Поиск при недоступной модели может дать лексические предложения, но не успешный подбор. Пустой рабочий каталог отвечает needs_data, readiness — 503 catalog_empty.

`needs_input`: нужна категория, исправление/уточнение факта, обязательное поле либо подтверждение единственного проверенного кода. Один кандидат сам по себе никогда не означает resolved.

`ambiguous`: несколько позиций, которые не удалось обоснованно различить. Одинаковые ограничения прекращают вопросы по значениям. Нельзя выбирать по рангу, названию или ручному коду при оставшейся неоднозначности. Подтверждённый смысловой разделитель должен быть представлен проверенным свойством каталога; текстовой эвристики нет.

`resolved`: категория подтверждена и её связь проверена, осталась единственная совместимая конечная позиция, её значения проверены, обязательные поля заполнены, вся область покрыта, блокирующих проблем нет, пользователь явно подтвердил именно этот код. У позиции без характеристик value_verified=false, resolved запрещён. Ручной выбор не исключает остальных кандидатов и не обходит несовместимость.

Во всех незавершённых статусах **selected_ktru_code=null**. Несовместимый/чужой/шаблонный confirmed_ktru_code — HTTP 422, без успешного результата.

## Полный сценарий frontend

1. POST исходного описания как в примере. С проверенной поставкой сервер возвращает categories и вопрос kind=category. Без поставки — needs_data с сохранёнными фактами и причиной.
2. Пользователь выбирает id из categories. Повторный POST содержит прежние поля и `confirmed_category_id` с выбранным id; answers может содержать `{ "question_id":"category", "value":"<id из ответа>", "unknown":false }`.
3. Получив вопрос свойства, frontend добавляет ответ, например `{ "question_id":"<id вопроса>", "value":"650", "unit":"Вт", "unknown":false }`, сохраняя полное состояние. Для «не знаю» value=null, unknown=true. Сервер снова проверяет все позиции.
4. Исправление: attributes пополняется `{ "id":"input:power", "name":"Мощность", "value":"700", "unit":"Вт", "replaces":"<id факта из ответа>" }`; confirmed_ktru_code=null. Повторная проверка может изменить кандидатов и вопросы.
5. При единственной доказанно совместимой позиции сервер возвращает needs_input, next_question.kind=code, понятный next_steps и selected_ktru_code=null. Frontend показывает кнопку подтверждения только при type_verified, ktru_unique, fields_complete, value_verified, catalog_scope_complete и отсутствии blocking_issues.
6. POST того же состояния с confirmed_ktru_code, скопированным из кандидата. При выполнении всех условий: resolved, verification.confirmed_by_user=true, selected_ktru_code равен коду. Результат отображается и копируется только из ответа сервера.

В примерах нет выдуманных официальных кодов: id и код берутся из реальной поставки/предыдущего ответа. Синтетические TEST-коды находятся только в tests/fixtures. Frontend сохраняет форму между шагами, показывает загрузку/HTTP-ошибки; AbortController и номер запроса предотвращают перезапись нового ввода устаревшим ответом.

## Ошибки

422 валидации Pydantic: `{"detail":[{"type":"...","loc":["body","..."],"msg":"...","input":"..."}]}`. 422 подтверждения: `{"detail":{"code":"incompatible_ktru_code","field":"confirmed_ktru_code","message":"Подтверждение не соответствует текущей области и фактам"}}`; также unknown_category/stale_source согласно полю ошибки.

429: detail.code=analysis_busy; повторите после завершения текущего анализа. 503 выполнения: detail.code=analysis_unavailable, безопасное сообщение без внутренних исключений. 503 readiness: detail.code — catalog_empty/resources_invalid/model_unavailable/search_unavailable. HTTP-ошибка никогда не заменяется фиктивным resolved.
