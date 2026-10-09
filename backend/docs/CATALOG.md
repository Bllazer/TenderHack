# Контракт проверенной поставки

Это описание потребителя данных, а не новый XML/XLSX-конвертер. Команда должна согласовать формат очищенной поставки с этими структурами; backend не подключает непроверенный tenderhack_dataset.zip. Перед публикацией поставки нужна сверка значений, знаков, OR, единиц, кодов и метаданных с источником.

Формальная JSON-схема доступна через `Catalog.model_json_schema()` в `app/catalog.py`. Дополнительные поля запрещены, неизвестные возможности нельзя молча игнорировать. Рабочий каталог имеет synthetic=false; synthetic=true разрешён только тестам.

## catalog.json (format_version=1)

- catalog_version: версия поставки; source_description и validation_report: происхождение и отчёт проверки.
- categories: id/name/search_text/position_codes/link_verified/coverage_verified/evidence. search_text содержит проверенное описание типа, а не правила выбора по числам. Подтверждённая область должна перечислять все конечные позиции напрямую либо через семейства. link_verified и coverage_verified требуют evidence, но их истинность должна быть проверена командой по источнику.
- positions: code/name/parent_code/is_template/status/okpd2/no_new_features/no_new_features_evidence/source_path/raw_json/properties/quarantined. status: actual/terminated/unknown. is_template и no_new_features — true/false/null, неизвестное не равно false. Исходные коды и метаданные сохраняются. parent_code указывает на явный шаблон. Циклы, неизвестные ссылки, дубликаты и quarantined=true отклоняются.
- properties: id/name/aliases/source/evidence/subject/value_type/required/verified/allowed/semantic_key. Идентификатор контекстный; одно имя не доказывает тождество. source: ktru/portal/spgz; subject: product/packaging; value_type: text/number/boolean/unsupported. required — true/false/null. Проверенные алиасы и semantic_key требуют содержательной сверки; verified требует evidence. Пустой allowed, unsupported и неизвестная обязательность не доказывают значения.
- allowed: kind=enum/number/interval; value, lower, upper, lower_inclusive, upper_inclusive, unit, raw_unit, okei_code, value_code, source. **Единица и value_code принадлежат каждому элементу.** Несколько элементов — OR. Границы десятичные строки, минус сохраняется. Интервал может иметь одну открытую сторону. Непустой source обязателен. Нижняя граница больше верхней и нечисловые/бесконечные значения отклоняются.

Числовые элементы объединяются только после проверки совместимых известных размерностей. `%` не Ом, об/мин не минуты; единица правила не подтверждает единицу числа пользователя. Разные/неизвестные единицы дают unknown. Пустые правила и непроверенные позиции не допускают resolved. codes_disjoint_on_values не поддерживается и не используется.

Если исходная конструкция не представима достоверно, используйте value_type=unsupported/verified=false и сохраните исходник; не превращайте её в придуманное ограничение. Весь карантин исключается из рабочей поставки до индексации. Неизвестные статусы/шаблоны делают покрытие неполным.

## Индексы и модель

`python -m app.prepare index <catalog-dir> --model-dir <model-dir>` создаёт offline:

- vectors.npy: нормализованная float-матрица N×384, порядок categories;
- tokens.json: лемматизированные BM25 документы в том же порядке;
- manifest.json: catalog_version, analyzer_version, model_id, model_revision (40-символьный SHA), sha256 для catalog.json/vectors.npy/tokens.json.

Модель — `intfloat/multilingual-e5-small`, локальные config/tokenizer/model.safetensors плюс model.meta.json с revision и SHA файлов. Downloader запускается отдельно до API. Запросы не скачивают веса. Смена каталога требует новой индексации и перезапуска процесса; несовпадение SHA/версий/размерности блокирует readiness. Snapshot загружается один раз, матрица и структуры доступны только для чтения. При отсутствии поставки рабочая версия — awaiting-validated-supply, покрытие 0.
