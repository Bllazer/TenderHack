# Доработка этапа 2 после независимого ревью

База: main fe578f6674e9b6200630520056961554dfdeb9a5. До изменения выполнены
git status и fetch origin main: расхождений с командой нет. ktru_clean/ не
изменена и не включена в git. Этап 3 не начат, интерфейс не изменён.

## Исправления и основания

В app/checks.py scalar и values теперь сравниваются одним каноническим набором
значений. Одноэлементный массив, скаляр и дубликаты равны. Числа приводятся в
совместимую размерность; единица отсутствующего физического факта не угадывается.
Для kind=3 порядок и повторы не меняют смысл; разные значения одиночного
свойства сохраняют conflicting_facts. AlgorithmC использует такое же сравнение
scalar/set, а числовые дубликаты 1 и 1.0 не становятся разными значениями.
У интервалов в сравнении C сохранены открытые/закрытые границы.
Исходные форматы, raw, span и provenance фактов не переписываются.

Целочисленность подтверждённого count проверяется после масштабирования единиц,
независимо от присутствия ОКЕИ в правиле. backend и C отклоняют 1.5 шт без
округления. 0.001 тыс. шт соответствует одному целому объекту; 0.0015 тыс. шт
не соответствует. Для счётных интервалов проверяются конечные границы после
масштабирования, строгие границы и полное покрытие не меняются. Счёт в парах
сохраняет отдельную размерность в обоих слоях: половина пары не превращается
в одну штуку. Дробные физические величины остаются допустимыми. Без происхождения
семантики count результат unknown; новое толкование чисел без ОКЕИ не добавлено.

Реальное основание — clean5, позиция 32.50.13.190-00244, характеристика
7a7ace3d-4 «Количество контейнеров в наборе», kind=1, is_required=true,
ОКЕИ 796 «Штука», value_code=a8ab478c-8, диапазон 0 ≤ X ≤ 5.
Рядом проверена физическая величина «Объем растворов дополнительных контейнеров»
с ОКЕИ 111: 150.5 мл допустимы, целочисленность на неё не распространяется.
Для лопаты проверено e27882bd-c «Крепление черенка», kind=2, optional,
«Верхнее» и «Нижнее» — действительно разные варианты.

Обе полные записи clean5 в [source-selection.json](source-selection.json)
сверены с оригинальными actual-XML ЕИС того же version: характеристики,
kind/choiceType, обязательность, значения/value_code, единицы, OR/границы,
parent_code, is_template и noNewFeatures/null. Результаты и XML:
[xml/sources.json](xml/sources.json), xml/*.xml. Адаптер поставки не изменялся.

## До и после через работающий Docker API

[before.json](before.json): 15 полных запросов и ответов исходного API.
Одинаковые «Верхнее» из текста и values (включая повтор) давали
unknown/conflicting_facts. 1.5 шт и values=["1.5"] давали compatible/allowed_range.
Пустой/перевёрнутый интервал уже отклонялся, дробный объём уже проходил.

[after.json](after.json) содержит 22 актуальных полных POST и расширенную проверку:
одинаковые scalar/values — compatible, отличающееся одиночное значение —
unknown/conflicting_facts; явная правка сохранённого ответа заменяет его и
проверяется заново. Наборы kind=3 с разным порядком и повторами проходят.
1000 мм из текста и values=["100","100.0"] см не конфликтуют;
110 см создаёт конфликт. 1.5 шт отклоняется с invalid_count и исключает код,
а целые значения, масштабирование, дробные физические величины и корректные
интервалы проверяются согласно своим правилам.
Подтверждение кода с 1.5 шт возвращает 422/incompatible_ktru_code.

Дополнительно повторены матрица десяти настоящих семейств этапа 2,
счётные/источниковые пограничные случаи и пять сценариев этапа 1.
Их полные HTTP-вводы и ответы находятся в after-matrix.json, edges.json,
stage1-repeat/after.json. Требования resolved и источник-блокировки сохранены.
Явное подтверждение категории здесь отделяет проверку сужения от качества поиска.
Это настоящий каталог и локальная E5, без mock модели; unit/браузерные фикстуры
в результатах ниже обозначены отдельно.

## Браузерная гонка

Старый flow.spec.ts ждал только события request повторной попытки. Последний
assert «нет заголовка resolved» мог выполниться ещё при loading=true, после чего
Playwright закрывал context, пока обработчик route продолжал fetch/json/fulfill.
Это объясняет ранее зарегистрированное Response has been disposed.
В этом проходе одиночный и десять повторных запусков старого теста с trace
прошли: гонка зависит от тайминга, нового падения с trace не заявляем.

[browser-before-trace.json](browser-before-trace.json) содержит извлечённые
тайминги десяти настоящих trace с SHA: финальный assert начинается до fulfill
повторного запроса, ожидания response нет. Полные локальные ZIP trace не включены
в git. [browser-after-trace.json](browser-after-trace.json) фиксирует порядок
после исправления: waitForResponse зарегистрирован до click, затем response.finished,
проверка status и JSON, aria-busy=false и видимый результат needs_data.
Все прежние assertions сохранены; sleep, retries и подавление ошибок не добавлены.
Исходники приложения менять не потребовалось; изменён только браузерный тест.

## Ресурсы и воспроизведение

Каталог остаётся clean5-7331f0732bc0ff5c, 60 087 позиций / 54 190 конечных.
Engine: stateless-pipeline-v4.1-canonical-count. Подготовленные правила и поисковые
документы не менялись; каталог, индекс и модель не пересобирались.
Полные версии/SHA и повторная сверка actual-хешей с manifest:
[artifacts.json](artifacts.json). vectors.npy:
90f2b326277872a7cd83b7f7e74e8b2046ec1d11e846cd1cb8c3c4b66f260ae9.
E5 revision: 614241f622f53c4eeff9890bdc4f31cfecc418b3.

Из backend:

```powershell
$env:RUN_E5_TESTS='1'
$env:RUN_CLEAN_SUPPLY_TESTS='1'
$env:CLEAN_SUPPLY_DIR='../ktru_clean'
.venv/Scripts/python.exe -X utf8 -m pytest -q -o cache_dir=.tools/cache-followup --basetemp=.tools/pytest-followup-complete
.venv/Scripts/python.exe -X utf8 -m algorithms.validation_tests
.venv/Scripts/python.exe -X utf8 tests/real_stage2_followup.py --output docs/reviews/stage2-followup/after.json --phase after
.venv/Scripts/python.exe -X utf8 tests/real_semantics_stage2.py --selection docs/reviews/stage2/selection.json --output docs/reviews/stage2-followup/after-matrix.json --phase after
.venv/Scripts/python.exe -X utf8 tests/real_semantics_edges_stage2.py --selection docs/reviews/stage2/selection-edges.json --output docs/reviews/stage2-followup/edges.json
$env:REVIEW_OUTPUT_DIR='docs/reviews/stage2-followup/stage1-repeat'
.venv/Scripts/python.exe -X utf8 tests/real_exclusion_stage1.py after
```

Из frontend: BACKEND_TEST_PYTHON указывает на backend/.venv/Scripts/python.exe;
`npx.cmd --yes pnpm@11.25.0 test:e2e --grep 'needs_data preserves' --repeat-each 10 --trace on`,
`npx.cmd --yes pnpm@11.25.0 test:e2e` и полный набор с `--trace on`;
`npm.cmd run build`. Браузерные тесты используют отдельный тестовый API/фикстуры,
а не заменяют HTTP-приёмку настоящей поставки.
Из корня: `docker compose build api`, `docker compose up -d api`.

Фактические итоги: pytest **372 passed**, без skipped, с обоими ресурсными
флагами; сохранены все 90 числовых/интервальных проверок. C/D **28/28**.
Браузерный набор **11/11** (также с trace), отдельный исправленный retry-тест
**10/10** повторов с trace; frontend build успешно. Браузерный набор снова
повторён на финальной версии backend и прошёл 11/11.
Реальная HTTP-приёмка: 22 POST доработки, 83 POST матрицы этапа 2 и
21 POST пограничных случаев, включая источник-блокировки; повтор этапа 1 —
5 сценариев с отказом подтверждения исключённых кодов, resurrected=0.
Docker API действительно пересобран и запущен, оставлен healthy;
live/ready/catalog/status/OpenAPI возвращают 200 с настоящими ресурсами и
согласованным engine_version ([runtime.json](runtime.json)).
Итоги команд отдельно сохранены в [checks.json](checks.json).
Frontend работает на http://127.0.0.1:8080, API на http://127.0.0.1:8000.

Изменены app/checks.py, app/units.py, app/schemas.py, algorithms/AlgorithmC.py,
tests/test_stage2_followup.py, tests/real_stage2_followup.py, docs/API.md,
данный набор доказательств и frontend/tests/flow.spec.ts.

Ограничения этапа 2 остаются: неподтверждённая числовая семантика, неподдержанные
правила и дефекты источника не превращены в verified. Полная поддержка каталога
не заявляется. Этап 3 не начат.
