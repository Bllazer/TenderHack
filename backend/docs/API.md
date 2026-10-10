# API v0.2 — stateless контракт frontend

Базовый URL `/api`. Vite и Docker nginx сохраняют префикс. OpenAPI: `/api/openapi.json`, Swagger: `/api/docs`. Сервер не хранит анализы: каждый POST передаёт полное текущее состояние, ответ полностью заменяет предыдущий результат.

## Маршруты

| Метод | Путь | Результат |
|---|---|---|
| GET | `/api/health`, `/api/health/live` | 200 `{"status":"ok"}` — процесс работает |
| GET | `/api/health/ready` | 200 status/catalog_version/engine_version либо 503 detail.code/catalog_version/missing_resources |
| GET | `/api/v1/catalog/status` | 200 categories/positions/catalog_version/ready/reason/catalog_loaded/missing_resources/templates/final_positions |
| GET | `/api/v1/catalog/positions/{code}` | 200 карточка правил локального snapshot; 404 detail.code=unknown_ktru_code |
| POST | `/api/v1/analyses` | 200 полный результат; 422 неверный ввод/подтверждение; 429 заняты слоты; 503 внутренняя ошибка |

Старые серверные GET сохранённого анализа, CRUD СТЕ, сохраняемый feedback и старые маршруты чтения категорий/правил удалены (404). POST не хранится. Новый GET карточки читает только неизменяемый каталог, без анализа или сессии; analysis_id, ste_id, title, raw_payload и старый контейнер result не используются. Frontend вызывает только POST анализа. Миграция не требуется: серверного хранилища нет.

`catalog_loaded` означает наличие позиций, `ready` — готовность анализа; это разные признаки. `templates` и `final_positions` — количества известных шаблонов и конечных позиций (неизвестный is_template не включается ни в одну группу). `missing_resources` — список недостающих компонентов/артефактов: category_mapping, search_index, extractor, checker, questions. Для clean (5) без ML-артефактов: catalog_loaded=true, positions=60087, templates=5897, final_positions=54190, categories=0, ready=false, reason=search_index_missing. Подбор не завершён даже при ручной передаче реального кода.

Изменения для frontend-команды: поля статуса каталога и readiness добавлены; POST и его поля сохранены. При необходимости показывайте отдельно «каталог загружен» и причину неготовности. needs_data нельзя показывать как найденный код; selected_ktru_code остаётся null. Frontend в этом изменении не редактируется.

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

description: непустой исходный текст до 12000 символов. attributes: до 100 объектов `{id, name, value?, values?, unit?, unknown?, subject?, replaces?}`. Скалярный value по умолчанию ""; для выбора нескольких значений передайте values (см. ниже). id уникален, до 128; name до 256, value до 512, unit до 64. subject: `product` (по умолчанию), `packaging`, `unspecified`. unknown по умолчанию false. `replaces` — идентификатор предыдущего извлечённого/ответного факта либо `fragment:start:end`, если пользователь явно исправляет/размечает фрагмент. Неприменимый идентификатор отклоняется: исправление не должно незаметно потеряться после смены текста. Ответ сохраняет original_facts и corrections; facts содержит действующее состояние.

Скобочные интервалы извлекаются вместе с явно указанной единицей: `Стол; Высота: [70; 80] см` → value=`[70;80]`, unit=`см`; raw/span сохраняют исходный фрагмент без изменений. Поддерживаются `[a;b]`, `(a;b)`, `[a;b)`, `(a;b]`, знаки минус и десятичная запятая. Пустые интервалы с равными границами и хотя бы одной открытой стороной не допускаются: проверка даёт incompatible/invalid_number, selected_ktru_code=null; подтверждение такого исключённого кода — 422. Они не нормализуются в точку. `[750;750]` — допустимая точка. Аналогичные пустые ограничения каталога отклоняются при загрузке (readiness 503 resources_invalid).

additional: до 100 строк по 1024 символа. answers: до 100 объектов `{question_id, value?, values?, unit?, unknown?}`, один текущий ответ на идентификатор. «Не знаю»: value=null, unknown=true. confirmed_category_id: до 128 символов, confirmed_ktru_code: до 64. candidate_limit: 1–100, по умолчанию 20; ограничивает выдачу, а не проверяемую область. Все остальные поля запрещены, включая клиентские verification/normalized_value: доказательства вычисляются заново.

Ответы предыдущей категории сохраняются как источник явной правки через replaces; в новой категории они не становятся действующими фактами или доказательством. Идентификатор вопроса сверяется со snapshot каталога.

При исправлении фактов сбрасывайте confirmed_ktru_code. При смене исходного текста сбрасывайте категорию, ответы и устаревшие replaces; при смене категории удаляйте её ответ и подтверждение кода. Свойства другой категории сохраняются как дополнительные сведения, но не считаются доказательством.

## Ответ

После confirmed_category_id извлечение повторяется по свойствам и допустимым
текстовым значениям **всей** выбранной области. Буквальное целое значение с
единственным контекстным назначением становится фактом с исходными raw/span.
Одинаковое значение разных свойств требует подтверждения назначения.
Никакие числовые значения или единицы из правил не подставляются во ввод.
Отрицание не становится положительным проверенным фактом.
Неявные «не совсем совковая», «совковая не нужна», «возможно совковая» и
«совковая?» сохраняются как unknown с полным исходным сегментом raw/span;
требуется явное уточнение. Исправление опечатки не снимает отрицание.

Опечатка допустимого текстового значения предлагает correction с
confirmation_required=true и issue `unresolved_value_correction`; исправление
не применено до ответа. ID вопроса `value:<hash>` учитывает категорию, факт,
исходное значение и набор вариантов. Передавайте options[].value без разбора:
`accept_value:<номер>` подтверждает конкретную пару «свойство: значение»,
`reject` сохраняет исходное сведение дополнительно без назначения свойства,
unknown=true сохраняет неопределённость. Старый `mapping:<hash>` и
`accept:<имя>` для исправления **имени** остаются совместимыми.
Подтверждение запускает полную проверку, включая противоречия. Изменение текста,
факта или категории делает прежнее подтверждение исправления неприменимым.
Проверенный факт закрывает вопрос свойства; явный answer или attributes.replaces
заменяет старое значение. Обычный additional/unparsed сам по себе не блокирует подбор.

Пример реальной поставки: POST
`{"description":"лопата совковая","confirmed_category_id":"25.99.29.120-00004"}`
содержит Fact name=`Тип лопаты`, value/raw=`совковая`, span=`[7,15]`.
`ловпата совкавая` и `Лопата; Тип лопаты: Совкавая` сначала возвращают
needs_input и correction с вариантом «Тип лопаты: Совковая», selected_ktru_code=null.
Повторите полное состояние с answers=[{question_id: полученный ID,
value: options[].value}]. После принятия значение проверено заново; после отказа
исходное значение сохранено в additional. В этой поставке обе ветки заканчиваются
needs_data из-за неподдержанных правил семейства, без повторных вопросов о полях.

### Карточка и структурированные блокировки

Семантика адаптера: `clean5-adapter-v2-kind3-applicability`; справочник чисел:
`numeric-semantics-v1`. Версия адаптера и справочника входит в catalog_version;
snapshot старой семантики не проходит проверку версии поставки.

Для `kind=3` InputFact и Answer принимают необязательное `values: string[]`
(1–50 непустых элементов, каждый до 512 символов) вместо скалярного value.
Одновременные непустой value и values — 422. Для сравнения фактов скалярное
`value="Верхнее"` и `values=["Верхнее"]` эквивалентны. Повторы одного значения
не создают конфликт; порядок и повторы в наборе kind=3 не меняют его смысл.
Разные значения одиночного свойства остаются конфликтом. Числа сравниваются
после приведения совместимых единиц, а не по написанию. Исходные value/values,
порядок, raw, span и источник факта сохраняются в ответе.

Пример полного запроса:

```json
{
  "description": "Лопата совковая",
  "confirmed_category_id": "25.99.29.120-00004",
  "attributes": [{"id": "features", "name": "Конструктивные особенности", "values": ["Черенок", "Ручка на черенке"]}],
  "answers": [],
  "additional": [],
  "confirmed_ktru_code": null
}
```

Fact возвращает values списком, value="", raw — исходный JSON списка,
source=input/answer, span=null. При скалярном вводе values=null; исходные текст,
фрагмент и span сохраняются. Повтор элемента не конфликтует. Все выбранные
элементы проверяются; `Check.elements` содержит `{value, outcome, reason,
value_codes}` для каждого. Недопустимый элемент даёт invalid_value/out_of_range,
исключает позицию, и её confirmed_ktru_code отклоняется с 422.
Несколько разных значений при kind=1/2 дают multiple_values_not_allowed.
Скалярная строка с запятыми разбирается как список только внутри текстового
kind=3, если в справочнике нет литералов с запятыми. Точное составное значение
сохраняется целиком. Для неоднозначной пунктуации передавайте values явно.
Backend никогда не назначает все allowed как факты товара.

Вопрос свойства имеет multiple=true только для kind=3. Ответ:
`{"question_id":"<id из ответа>","values":["Черенок","Ручка на черенке"]}`.
Он заменяет старый факт этого свойства и запускает полную проверку.
Текущий ответ свойства имеет приоритет и над attributes того же свойства:
устаревший набор не объединяется с новым; raw прежнего выбора сохраняется
в corrections. Для самостоятельной правки attributes используйте replaces.
Явное `attributes.replaces="answer:<question_id>"` заменяет сохранённый ответ
и имеет приоритет над ним, чтобы редактор мог исправить ранее данный ответ.
unknown=true сохраняет неопределённость даже при переданном списке.
choice_type сохраняется как метаданные ведения КТРУ и не заменяет kind.

BlockingReason дополнен applicability=source/required/provided/potential и
blocks_resolution. Недостоверный источник блокирует независимо от заполнения.
Непроверяемые обязательное поле, введённое значение и конкурирующая unknown-позиция
не позволяют доказать соответствие/уникальность. Невведённое необязательное
поле имеет applicability=potential, blocks_resolution=false; оно не становится
проверенным, его Check остаётся unknown/not_provided. Единственность нельзя
доказать, игнорируя конкурирующий код. Полезные вопросы карточки продолжаются
даже при оставшейся блокировке; вопрос подтверждения кода требует готового proof.
GET карточки не знает ввода: provided не назначается; необязательные ограничения
показаны как потенциальные. Только reasons с blocks_resolution=true блокируют.

Число без ОКЕИ проверяется только при явных numeric_semantics=count/designation
и numeric_semantics_evidence; иначе numeric_semantics_unknown/unknown.
Для подтверждённой семантики count целочисленность проверяется и при исходном
ОКЕИ, и без него, после учёта масштаба единицы. Например, 0.001 тыс. шт = 1 шт
допустимо, 1.5 шт и 0.0015 тыс. шт дают incompatible/invalid_count; округления
нет. Для счётных интервалов конечные границы также должны быть целыми после
масштабирования; открытость границ и проверка полного покрытия сохраняются.
Счёт пар сохраняет размерность исходной единицы «пар»; половина пары не
подтверждается преобразованием в одну штуку.
Дробные физические величины не запрещены. Без подтверждённого происхождения
numeric_semantics=count результат unknown/numeric_semantics_unknown.
Физическая единица не подменяет счёт, неверная размерность
даёт unit_mismatch. Для физических величин отсутствие единицы ввода остаётся
unit_missing; единица схемы не доказывает единицу пользователя. Источник, OR,
строгие границы и value_code сохраняются. source_issues/resolve_blocked и позиции
без характеристик сохраняют запрет resolved.

GET `/api/v1/catalog/positions/25.99.29.120-00000001` возвращает
`{catalog_version, position, blocking_reasons}`. position содержит code/name/okpd2,
parent_code/is_template/status/version/source_status, no_new_features (включая null),
no_new_features_evidence, source_path, raw_json, quarantined, source_issues,
resolve_blocked/resolve_blocked_reason и properties. Это **правила КТРУ**;
характеристики пользователя находятся только в facts POST, не в карточке.

Property: id/name/aliases/source/evidence/subject/value_type/required/verified,
semantic_key/kind/source_type/choice_type/raw_json/unsupported_reasons/allowed,
numeric_semantics (physical/count/designation/unknown), numeric_semantics_evidence.
AllowedValue: kind(enum/number/interval), value/lower/upper, lower_inclusive/
upper_inclusive, unit/raw_unit/okei_code/value_code/source. Элементы allowed
соединены OR; единица и код сохраняются отдельно для каждого элемента.
При compact-raw raw_json свойства содержит указатель на исходную характеристику
в position.raw_json, который сохраняет запись поставки целиком.

BlockingReason (в карточке и Candidate): code/explanation/property_id/property_name/
kind/units/unsupported_reasons/source_issues/resolve_blocked_reason/rule_source.
Пустые поля — null/[]; source_issues — строки JSON исходной поставки.
units отражает исходные или нормализованные единицы сохранённых допустимых
элементов, null означает отсутствие единицы; полный первоисточник есть в raw_json.
Причины относятся к данным/поддержке семантики и не исчезают после ручного выбора.
source_issues и resolve_blocked_reason пусты у трёх рассматриваемых лопат;
В адаптере v2 kind=3 поддержан: «Конструктивные особенности» проверяются
по каждому действительно выбранному значению. Количество бортов и ребер
без ОКЕИ интерпретируется отдельным проверенным справочником как счётное;
исходные unit/raw_unit/okei_code остаются null. Основания — actual-XML и
описание XML, сохранённые в docs/reviews/stage2. Другие неоднозначные числа
без ОКЕИ остаются неподдержанными; единицы не назначаются по умолчанию.

Всегда: status, original_text, normalized_text, original_facts, facts, unparsed, additional, categories, candidates, excluded, candidate_count, excluded_count, scope, issues, corrections, next_question, next_steps, verification, selected_ktru_code, catalog_version, engine_version.

Текущая версия проверки: `stateless-pipeline-v4.1-canonical-count`.
Формат HTTP-схем сохранён; исправлены сравнение эквивалентных фактов и проверка счёта.

- Fact: id/name/value/values/unit/raw/span/source/subject/negated/unknown/normalized_value/normalized_unit/mappings. source: text/input/answer. span — полуоткрытый диапазон символов исходного текста либо null. Нормализация не меняет исходный факт.
- Fragment: id/raw/span. Нераспознанные и дополнительные сведения не теряются.
- CategoryCandidate: id/name/rank/rank_score/reasons. rank_score — RRF, не вероятность. Предложение требует подтверждения типа пользователем.
- Candidate: ktru_code/name/okpd2/outcome/checks/missing_required/has_characteristics/**blocking_reasons**/**exclusion_reasons**. outcome: compatible/incompatible/unknown. Check: property_id/name/outcome/reason/fact_ids/value_codes/elements. Шаблон не становится конечным кандидатом. Допустимые элементы сохраняют свои единицы и value_code в каталоге.

Достоверный incompatible-check применимого проверенного правила исключает позицию,
даже если другое правило неподдержано, не заполнено или источник запрещает resolved.
Такая позиция находится в excluded, а не candidates; checks и blocking_reasons сохраняются.
exclusion_reasons — массив Issue с исходным фактом, идентификатором/происхождением
правила и объяснением противоречия. Для оставшихся позиций массив пуст.
Эти диагностические ошибки исключённых позиций не блокируют проверку оставшегося пула.
Правило verified=false, с unsupported_reasons или без доказанного происхождения
не доказывает исключение. Отсутствие противоречий само по себе не даёт compatible:
неподдержанные ограничения оставшейся позиции сохраняют unknown и запрет resolved.
Подтверждение исключённого кода возвращает 422 incompatible_ktru_code.
- candidate_count/excluded_count — полные количества до лимита ответа. scope: category_id/position_count/evaluated_count/complete. Весь конечный пул выбранной области проверяется до ограничения UI.
- Issue: code/severity/explanation/fact_id/property_id/candidate_code/rule_source/suggested_action/confirmation_required. severity: info/warning/error. Correction: fact_id/kind/original/normalized/explanation/confirmation_required; kind: auto_normalization/proposed_text_fix/proposed_fact_change. Безопасное преобразование единицы показывается; изменение факта требует пользовательского ввода.
- next_question: null либо id/kind/title/reason/property_name/options/unit/allow_unknown; kind: conflict/category/property/code/correction; options: value/label. По одному вопросу. Неизвестный ответ не подтверждает значение, уже отвеченный вопрос не повторяется бесконечно.

Варианты вопроса свойства объединяются из проверенных правил всех оставшихся позиций той же семантики и объекта (товар/упаковка); правила разных объектов не смешиваются.
- verification: type_verified/ktru_unique/fields_complete/value_verified/catalog_scope_complete/value_indistinguishable/confirmed_by_user/blocking_issues — boolean. confirmed_by_user отражает присланное явное подтверждение кода, а не успешность проверки.

## Статусы и подтверждение

Версия движка: `stateless-pipeline-v4-ktru-multiselect-applicability`. A использует действующие правила извлечения с контекстными enum; B — существующий локальный Search. Для пригодного snapshot с категориями C/D подключены через algorithm_adapter и app.structured. C дополняет строгую проверку app.checks и не может самостоятельно разрешить подбор. Backend вычисляет verification и selected_ktru_code заново; флаги и selected из AlgorithmC не принимаются как доказательство.

### Неподтверждённая правка имени характеристики

Потенциально значимое несопоставленное имя (например «Мошность» рядом с правилом «Мощность») создаёт issue `unresolved_property_correction`, severity=error, confirmation_required=true. Ответ — needs_input, selected_ktru_code=null и next_question.kind=correction. Обычные additional/unparsed сами по себе не блокируют результат.

Передайте ответ в существующем answers, сохранив полное состояние:

```json
{"description":"Дрель; Питание: от сети; Мощность:650 Вт; Мошность:900 Вт",
 "confirmed_category_id":"tools","confirmed_ktru_code":"TEST-DRILL-MAINS",
 "answers":[{"question_id":"<id из next_question>","value":"accept:Мощность","unknown":false}]}
```

Этот пример использует только синтетический тестовый каталог. Допустимые value копируйте из options: `accept:<имя>` подтверждает сопоставление, `reject` оставляет исходный факт дополнительным без назначения свойства. `unknown=true` оставляет проблему неразрешённой. При подтверждении значения и единицы не меняются: 900 Вт полностью перепроверяется вместе с 650 Вт, возникает conflicting_facts и завершение запрещено. Подтверждённая правка остаётся в corrections с confirmation_required=false; исходные raw/span/original_facts сохраняются.

ID вопроса зависит от категории, факта, значения, единицы и предложения. После правки или смены категории старый ответ не действует. Явная правка через attributes.replaces остаётся доступной. Frontend-команде нужно поддержать новый kind=correction, показывать options и отправлять их value в answers; frontend в этом коммите не меняется.

Issue `algorithm_verification_incomplete` означает, что C не подтвердил результат строгой проверки. Непроверенный конкурент остаётся unknown в полном пуле и не доказывает уникальность совместимого кода.

ktru_unique=true требует единственного compatible, а не единственного unknown. Пока значимая правка не разрешена, value_verified=false. Сопоставление и предложение правки учитывают объект характеристики (товар/упаковка).

`needs_data`: нет пригодного каталога/индекса/модели или недостаточно подтверждённых ограничений/покрытия. Поиск при недоступной модели может дать лексические предложения, но не успешный подбор. Пустой рабочий каталог отвечает needs_data, readiness — 503 catalog_empty.

`needs_input`: нужна категория, исправление/уточнение факта, обязательное поле либо подтверждение единственного проверенного кода. Один кандидат сам по себе никогда не означает resolved.

`ambiguous`: несколько позиций, которые не удалось обоснованно различить. Одинаковые ограничения прекращают вопросы по значениям. Нельзя выбирать по рангу, названию или ручному коду при оставшейся неоднозначности. Подтверждённый смысловой разделитель должен быть представлен проверенным свойством каталога; текстовой эвристики нет.

`resolved`: категория подтверждена и её связь проверена, осталась единственная совместимая конечная позиция, её значения проверены, обязательные поля заполнены, вся область покрыта, блокирующих проблем нет, пользователь явно подтвердил именно этот код. У позиции без характеристик value_verified=false, resolved запрещён. Ручной выбор не исключает остальных кандидатов и не обходит несовместимость.

Во всех незавершённых статусах **selected_ktru_code=null**. Несовместимый/чужой/шаблонный confirmed_ktru_code — HTTP 422, без успешного результата.

## Полный сценарий frontend

Если в оставшемся пуле есть позиция без характеристик, с `source_issues`, запретом
источника или неподдерживаемым правилом, вопросы заполнения полей и подтверждения кода прекращаются: ответ
теперь `needs_data`, а не тупиковый `needs_input`. Issue
`catalog_constraints_unavailable` и `next_steps` требуют поддержки указанной
семантики, уточнения исходных единиц/данных либо смены типа товара.
`next_question=null`, `selected_ktru_code=null`;
ручное подтверждение не устраняет дефект данных. Готовность ресурсов сервиса
(`ready=true`) не гарантирует проверяемость ограничений каждой позиции.
Конкретное неразрешённое исправление или конфликт остаётся вопросом correction/conflict
и needs_input; после его разрешения блокировка каталога сохраняется.

1. POST исходного описания как в примере. С проверенной поставкой сервер возвращает categories и вопрос kind=category. Без поставки — needs_data с сохранёнными фактами и причиной.
2. Пользователь выбирает id из categories. Повторный POST содержит прежние поля и `confirmed_category_id` с выбранным id; answers может содержать `{ "question_id":"category", "value":"<id из ответа>", "unknown":false }`.
3. Получив вопрос свойства, frontend добавляет ответ, например `{ "question_id":"<id вопроса>", "value":"650", "unit":"Вт", "unknown":false }`, сохраняя полное состояние. Для «не знаю» value=null, unknown=true. Сервер снова проверяет все позиции.
4. Исправление: attributes пополняется `{ "id":"input:power", "name":"Мощность", "value":"700", "unit":"Вт", "replaces":"<id факта из ответа>" }`; confirmed_ktru_code=null. Повторная проверка может изменить кандидатов и вопросы.
5. При единственной доказанно совместимой позиции сервер возвращает needs_input, next_question.kind=code, понятный next_steps и selected_ktru_code=null. Frontend показывает кнопку подтверждения только при type_verified, ktru_unique, fields_complete, value_verified, catalog_scope_complete и отсутствии blocking_issues.
6. POST того же состояния с confirmed_ktru_code, скопированным из кандидата. При выполнении всех условий: resolved, verification.confirmed_by_user=true, selected_ktru_code равен коду. Результат отображается и копируется только из ответа сервера.

В примерах нет выдуманных официальных кодов: id и код берутся из реальной поставки/предыдущего ответа. Синтетические TEST-коды находятся только в tests/fixtures. Frontend сохраняет форму между шагами, показывает загрузку/HTTP-ошибки; AbortController и номер запроса предотвращают перезапись нового ввода устаревшим ответом.

## Ошибки

422 валидации Pydantic: `{"detail":[{"type":"...","loc":["body","..."],"msg":"...","input":"..."}]}`. 422 подтверждения: `{"detail":{"code":"incompatible_ktru_code","field":"confirmed_ktru_code","message":"Подтверждение не соответствует текущей области и фактам"}}`; также unknown_category/stale_source согласно полю ошибки. template_confirmation запрещает выбор известного шаблона, в том числе до подключения поиска.

429: detail.code=analysis_busy; повторите после завершения текущего анализа. 503 выполнения: detail.code=analysis_unavailable, безопасное сообщение без внутренних исключений. 503 readiness: detail.code — catalog_empty/resources_invalid/model_unavailable/search_unavailable/search_index_missing/extractor_missing/checker_missing/questions_missing; detail.catalog_version и detail.missing_resources указывают snapshot и недостающие компоненты. HTTP-ошибка никогда не заменяется фиктивным resolved. source_issue/resolve_blocked/verification_incomplete в issues блокируют завершение, а не исправляются подтверждением кода.

## Реальный HTTP-пример clean (5) без поисковых артефактов

```http
POST /api/v1/analyses
Content-Type: application/json

{"description":"Чай черный (ферментированный)","confirmed_ktru_code":"10.83.13.120-00000003"}
```

HTTP 200: status=needs_data, issues[0].code=search_index_missing, categories/candidates=[], candidate_count=0, scope.complete=false, verification.type_verified/value_verified/catalog_scope_complete=false, verification.confirmed_by_user=true, verification.blocking_issues=true, selected_ktru_code=null. Подтверждение отражается как действие пользователя, а не доказательство. next_steps требует category_mapping и search_index. Полный фактический ответ Compose: [examples/clean5-needs-data.json](examples/clean5-needs-data.json).
