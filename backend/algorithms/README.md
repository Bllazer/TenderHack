# algorithms — алгоритмы C и D

Два алгоритма поверх очищенного справочника КТРУ (`ktru_clean`). Только стандартная библиотека Python, внешних зависимостей нет.

* **`AlgorithmC.py`** — строгая совместимость и сужение пула конечных кодов КТРУ.
* **`AlgorithmD.py`** — выбор следующего уточняющего вопроса, когда C не смог оставить один код.

Место в пайплайне:

```
текст товара ──► A (модель): факты ──► сопоставление свойств ──┐
                                                               ▼
пул кандидатов от B ────────────────────────────────────► AlgorithmC ──► код не уникален ──► AlgorithmD
                                                               │                                  │
                                                          CResult → backend/UI              Question → UI
                                                                                                  │
                                                                ответ пользователя (тот же Fact) ◄┘
```

Оба алгоритма — чистые функции над данными: ни HTTP, ни базы, ни модели внутри нет.
Всё, что нужно на входе, — пул кодов и список фактов.

---

## Быстрый старт

Команды выполняются **из каталога `backend/`** (там, где лежит пакет `algorithms/`), а не из корня репозитория и не изнутри пакета:

```bash
cd backend

python -m algorithms.validation_tests     # проверки C и D — 28 тестов, полный корпус не нужен
python -m algorithms.run_example algorithms/samples/example_case_full.json       # кейс «дрель от сети»
python -m algorithms.run_example algorithms/samples/example_case_question.json   # кейс «спросить тип питания»
```

Ожидаемый конец вывода проверок:

```
Ran 28 tests in 0.1s

OK
```

---

## Что чем запускать

| Команда (из `backend/`) | Что делает | Нужен корпус |
| --- | --- | --- |
| `python -m algorithms.validation_tests` | все проверки C и D | нет |
| `python -m algorithms.run_example algorithms/samples/example_case_full.json` | один кейс из JSON: вердикты C, затем вопрос D | нет |
| `python -m algorithms.run_example algorithms/samples/example_case_question.json` | тот же пул без «Тип питания» — D должен спросить | нет |
| `python -m algorithms.validation_run --fixture --cards 20` | сквозной прогон A → C → D на мини-справочнике (дымовая проверка) | нет |
| `python -m algorithms.validation_run --cards 120` | полный прогон на карточках СТЕ, пишет `algorithms/validation_report.json` | **да** |

Аргументы `run_example`: `--fixture` (брать мини-справочник) и `--json` (вывести результат как JSON).

---

## Корпус КТРУ

Тестам и примерам полный корпус не нужен: они идут на мини-справочнике
`algorithms/samples/ktru_mini_index.json` (188 позиций, 6 семейств).

Полный корпус `ktru_actual_all.jsonl.gz` (14 МБ, 60 087 позиций) **намеренно не коммитится** —
он перечислен в `algorithms/.gitignore`. Он нужен только для `validation_run --cards`.
Положить его:

```bash
# из архива организаторов (КТРУ xml.zip → nsiKTRUNew_all_actual_*.xml) или из папки команды
cp ktru_actual_all.jsonl.gz backend/algorithms/data/
```

`KtruIndex.load()` ищет файл в `algorithms/data/`, `KtruIndex.load("путь")` принимает явный путь.
Словари единиц (`okei_units.json`, `example_unit_strings.json`) в репозитории есть — они нужны всегда.

---

## Использование из кода

```python
from algorithms import AlgorithmC, AlgorithmD, KtruIndex, Units, Fact, Unit

index = KtruIndex.load()                    # нужен корпус в algorithms/data/
# без корпуса, для проверок и экспериментов:
# index = KtruIndex.load_json("algorithms/samples/ktru_mini_index.json")

units = Units.load()

# 1. пул конечных позиций раскрывает стадия B (поиск + связи)
pool = index.family_pool("28.24.11.000-00000003")        # «Дрель ручная электрическая»

# 2. факты приходят от модели (стадия A) после сопоставления свойств
facts = [
    Fact(kind="text",   value="От сети", name="Тип питания", extraction_confidence=0.95),
    Fact(kind="number", value=0.65, name="Мощность",
         unit=Unit(code="2355", text="кВт"), extraction_confidence=0.9),
]

result = AlgorithmC.run_c(pool, facts, index, units, type_verified=True)
result.survivors              # ['28.24.11.000-00000001']
result.flags.to_dict()        # ktru_unique, value_verified, fields_complete, blocking_issues…
result.to_dict()              # готовый к отдаче JSON

# 3. если код не уникален — спрашивает D
question = AlgorithmD.next_question(
    result.survivor_positions(index),          # пул, оставшийся после C
    AlgorithmD.answered_from_facts(facts),     # что уже спрошено, включая definition_alternatives
    index,
    type_verified=True,
    blocking_issues=result.flags.blocking,
)
question.to_dict()            # вопрос либо stop_reason
```

Короткие импорты доступны и напрямую: `from algorithms import run_c, next_question, answered_from_facts`.

### Подписи функций

```python
run_c(pool, facts, index, units=None, *, type_verified=False,
      scenario_date="2026-10-09", catalog_scope_complete=True, max_candidates=50) -> CResult

next_question(pool, answered, index, *, type_verified=False,
              blocking_issues=None, fill_required=True) -> Question

answered_from_facts(facts) -> set
```

`max_candidates=0` — вернуть всех кандидатов без обрезки (нужно, когда результат уходит в D).

---

## Формат факта (вход от модели)

Обязательные поля: `def_id`, `kind`, `value`, `unit`, `extraction_confidence`, `polarity`.
Необязательные: `name`, `operator`, `negation`, `span`, `extraction_method`, `definition_alternatives`, `fact_id`.

```json
{ "fact_id": "f7", "def_id": "5c54e4eb-4", "name": "Максимальный кровоток, л/мин",
  "kind": "number", "value": 0.65, "unit": { "code": "2355", "text": "кВт" },
  "operator": null, "polarity": "affirmed", "negation": null,
  "extraction_confidence": 0.86, "span": [12, 22] }
```

* `kind`: `number`, `text`, `boolean`, `range` (`{"min":…,"max":…}`), `number_set`, `text_set`.
* `operator`: `>=`, `<=`, `>`, `<`, `=`, `!=` — для «не менее 2», «до 60».
* Единицу переводить не нужно: `кВт → Вт` делает C. Присылать как в тексте плюс код ОКЕИ, если он известен.
* `extraction_confidence < 0.6` — факт проверяется, но **не исключает** код.
* Строковый мост `fact_from_string("Мощность: 0,65 кВт")` даёт уверенность 0.5: работает, пока модель отдаёт строку, и не ломает точность молча.

---

## Что возвращает C → `CResult`

```json
{ "schema_version": "1.0", "catalog_version": "ktru_clean_v3.1",
  "pool": ["28.24.11.000-00000001", "28.24.11.000-00000002"],
  "counts": { "before": 2, "compatible_after": 1, "unknown_only": 0,
              "incompatible": 1, "blocked_excluded": 0 },
  "flags": { "type_verified": true, "ktru_unique": true, "fields_complete": false,
             "value_verified": true, "catalog_scope_complete": true,
             "value_indistinguishable": false, "blocking_issues": [] },
  "selected_ktru_code": null,
  "candidates": [
    { "code": "28.24.11.000-00000002", "verdict": "incompatible",
      "exclusions": [ { "fact_id": "f1", "property": "Тип питания",
                        "actual": "От сети", "rule": "допустимо: \"От аккумулятора\"",
                        "source": { "def_id": "d8d81584-b",
                                    "position": "28.24.11.000-00000002" } } ],
      "unresolved": [], "missing_required": [], "resolve_blocked": false, "future": false } ],
  "trace": [ { "fact_id": "f1", "property": "Тип питания", "before": 2, "after": 1 } ] }
```

* `verdict` — `compatible` | `incompatible` | `unknown`. **`unknown` не исключает позицию.**
* `exclusions` — основание для сообщения пользователю: свойство, введённое значение, правило позиции.
* `unresolved` — что не проверено и почему: `not_defined_in_position`, `unit_dimension_unsupported`,
  `name_ambiguous_in_position`, `low_confidence_fact`, `user_unit_missing`, `unsupported_value_shape`.
* `missing_required` — обязательные свойства позиции, по которым нет факта (нужно для полноты карточки).
* `selected_ktru_code` — заполняется только при `type_verified ∧ ktru_unique ∧ fields_complete ∧
  value_verified ∧ catalog_scope_complete` и отсутствии блокировок. Подтверждение пользователя не обходит ограничения.

## Что возвращает D → `Question`

```json
{ "question": { "def_id": "d8d81584-b", "name": "Тип питания", "reason": "splits_candidates",
                "answer_kind": "choice", "unit": null,
                "choices": [ { "value": "От сети", "candidates": null },
                             { "value": "От аккумулятора", "candidates": null } ],
                "bounds": null,
                "expected_effect": { "candidates_before": 2, "buckets": 2, "worst_case": 1 },
                "fallback": { "on_unknown": "keep_candidates" } },
  "alternatives": [], "stop_reason": null }
```

* `answer_kind` — как строить поле ввода: `choice`, `number`, `boolean`, `text_set`, `number_set`.
* `bounds` — границы для числового вопроса, `unit` — в каких единицах ждём ответ.
* `reason` — `splits_candidates` | `fixes_conflict` | `confirms_type` | `fills_required` | `meaningful_difference`.
* `stop_reason` — почему вопроса нет: `already_unique`, `value_indistinguishable`, `no_splitting_property`,
  `no_candidates`, `blocking_issue:<список>`.
* Ответ пользователя возвращается **тем же форматом факта** (с `extraction_confidence = 1.0`) и передаётся
  в следующий вызов C вместе с остальными фактами.

---

## Что зашито в C

1. Исключает код только подтверждённая несовместимость. Отсутствие факта, отсутствие свойства в позиции,
   неизвестная единица и низкая уверенность дают `unknown`.
2. Перевод только внутри размерности: `кВт → Вт` можно, `кг → Вт` нельзя. Контекстные алиасы
   (`мин`, `мин-1`, `с`) без контекста характеристики не применяются.
3. Строгие границы: `>1.25` отсекает ровно `1.25`, `≤1.25` её принимает. Односторонние диапазоны
   (`X < 60`) остаются односторонними.
4. Пользовательский интервал должен целиком лежать внутри каталожного; частичное пересечение — не подтверждение.
5. Альтернативы одного свойства — «ИЛИ»; альтернативы с разными единицами не смешиваются.
6. Если `def_id` не найден в позиции, свойство ищется по имени; при неоднозначном имени
   (`Объем` с двумя кодами и разными правилами) — `name_ambiguous_in_position`, а не исключение.
7. Позиции с `resolve_blocked` (шаблоны, без характеристик, дефект источника) из сужения исключаются
   и считаются отдельно в `counts.blocked_excluded`.
8. Значение — по структуре (`values` / `ranges` с их единицами), а не по разбору текста подсказки.

## Что зашито в D

1. Порядок: конфликт/блокировка → подтверждение типа → свойство-разделитель → добор обязательных полей.
2. Группировка по **множеству** допустимых значений, а не по принципу «значения разные»: ответ, совместимый
   с большинством кандидатов, вопросом не считается.
3. Свойство спрашивается, если оно сужает оставшихся; позиции без этого свойства ответ не исключает
   и учитываются в худшем случае.
4. Одинаковая структура ограничений у всех кандидатов — вопросы по значениям запрещены
   (`value_indistinguishable`), нужен смысловой признак или статус `ambiguous`.
5. Уже заданные свойства (включая `definition_alternatives`) не спрашиваются повторно.
6. «Не знаю» сохраняет неопределённость: `fallback.on_unknown = keep_candidates`.

---

## Тесты

`python -m algorithms.validation_tests` — 28 проверок: синтетические позиции плюс мини-справочник.
Покрыто: деление пула по значению, отсутствие свойства и отсутствие факта, строгие границы, диапазоны,
перевод единиц, несовместимая размерность, низкоуверенный факт, конфликт фактов, заблокированные позиции,
неоднозначное имя в позиции, отсутствие резолва без подтверждения типа, порядок вопросов D, повторный вопрос,
остановки D, строковый мост.

`python -m algorithms.run_example algorithms/samples/example_case_full.json` — показательный кейс:
пул из двух позиций, «Тип питания = От сети» и «Мощность = 650 Вт»; ожидаемый вывод описан в
`algorithms/samples/expected_output.md` (там же таблицы «что проверять»).

---

## Если команда не работает

**`ModuleNotFoundError: No module named 'algorithms'`**
Команда запущена не из `backend/` (например, из корня репозитория или изнутри `algorithms/`).
Перейдите в `backend` и повторите:

```bash
cd backend
python -m algorithms.validation_tests
```

**`ImportError: attempted relative import with no known parent package`**
Файл запущен напрямую (`python validation_tests.py`). Так нельзя — файл часть пакета.
Используйте `python -m algorithms.validation_tests` из `backend/`.

**`FileNotFoundError: не найден ktru_actual_all.jsonl.gz`**
Нет полного корпуса (он не коммитится). Либо положите файл в `backend/algorithms/data/`,
либо работайте без него: `python -m algorithms.validation_tests` и
`python -m algorithms.validation_run --fixture --cards 20`.

**`FileNotFoundError: не найден okei_units.json`**
Потеряна папка `backend/algorithms/data/` со словарями (`okei_units.json`, `example_unit_strings.json`).

**`PermissionError: [Errno 13] Permission denied: ... validation_report.json`**
`validation_run` пишет отчёт рядом с пакетом (`algorithms/validation_report.json`), а этот файл
перечислен в `algorithms/.gitignore`. Ошибка значит, что каталог пакета защищён от записи
(например, запуск из песочницы/контейнера только для чтения). Варианты: запускать из копии
пакета, куда есть запись, или временно исключить строку из `.gitignore`. Сами проверки
(`validation_tests`, `run_example`) отчёт не пишут и работают всегда.

**`validation_run --fixture` пишет «ни одной карточки не обработано»**
Это ожидаемо: мини-справочник покрывает синтетические кейсы, но не карточки СТЕ (в нём 6 семейств).
Режим `--fixture` годится как дымовая проверка прогона, а не как метрика по карточкам. Для цифр
нужен полный корпус и запуск без `--fixture`.

**Кириллица в консоли Windows отображается кракозябрами**
Это кодировка консоли, а не ошибка данных:

```powershell
$env:PYTHONIOENCODING="utf-8"
```

---

## Состав папки

| Файл | Что это |
| --- | --- |
| `AlgorithmC.py` | алгоритм C: проверки значений, диапазоны, единицы, вердикты, семь флагов, трасса |
| `AlgorithmD.py` | алгоритм D: выбор вопроса, остановки |
| `contract.py` | контракт данных: `Fact`, `Pool`, `CResult`, `Question`, `Unit`, строковый мост |
| `ktru_index.py` | индекс справочника: позиции, семейства, типизированные ограничения |
| `run_example.py` | запуск одного кейса из JSON |
| `validation_tests.py` | 28 проверок C и D |
| `validation_run.py` | сквозной прогон A → C → D |
| `data/` | словари единиц; корпус `ktru_actual_all.jsonl.gz` — по желанию, не коммитится |
| `samples/` | мини-справочник, кейсы, ожидаемый вывод, карточки СТЕ |

## Границы

* Матрица единиц (`DIMENSIONS` в `AlgorithmC.py`) покрывает 25 размерностей, встречающихся в КТРУ;
  алиасы из `example_unit_strings.json` закрывают 81,7 % написаний из примеров. Неизвестная единица
  возвращается как неопределённая, а не подставляется по похожести.
* Поиск пула в `validation_run.py` — грубая заглушка вместо BM25 + эмбеддингов: пересечение слов
  названия с именами характеристик семейства. Настоящий поиск — стадия B, отдельная задача.
* Дефекты исходной выгрузки (7 позиций с невыполнимыми диапазонами) не исправляются: позиция
  блокируется через `resolve_blocked` и не участвует в сужении.
* Диагностика E и альтернативы F в этой поставке не реализованы — они получают от C готовые данные
  (`flags.blocking`, `candidates[].exclusions`).
* Числа валидации на карточках СТЕ зависят от стадии сопоставления свойств: в текущей заглушке
  `def_id` получают 20–30 % фрагментов. По мере готовности модели и словаря свойств этот процент
  определяет качество всей цепочки, а не алгоритмы C и D.
