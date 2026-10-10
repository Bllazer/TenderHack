# Подключение алгоритмов к stateless backend

Текущие модули: extraction.extract/apply_inputs, search.Search.find и E5Encoder, checks.evaluate, questions.next_question, prepare (существующая offline-подготовка). Они не переписаны. Порты в app/components.py адаптируют их; HTTP-оркестрация в app/analysis.py.

| Компонент | Вход → выход | Подключение |
|---|---|---|
| Extractor.extract | исходный текст, tuple имён из snapshot → ExtractionResult(facts, unparsed, corrections) | Components.extractor |
| Searcher.find_state | SearchInput(description, tuple текущих facts после ответов/правок) → list CategoryCandidate | app.state.search в lifespan; существующий Search.find(text) оборачивается ExistingSearcher |
| Checker.evaluate | Position, текущие list Fact, unit_resolver(property, raw_unit) → Candidate, list Issue, list Correction | Components.checker |
| questions | category_id, категории, конечные позиции, кандидаты, факты, issues, answered, ready, confirmed_code → next_question/null, value_indistinguishable | Components.questions, текущая сигнатура questions.next_question |

Создавайте Components с реализациями в lifespan после единовременной загрузки ресурсов. Отсутствующий компонент передавайте как None: readiness=503 и анализ=needs_data с extractor_missing/checker_missing/questions_missing. Отсутствующий поиск или артефакт аналогично блокирует завершение. Неожиданное исключение приводит к безопасному HTTP 503 analysis_unavailable; внутренние детали остаются в серверном журнале. Работа синхронная в thread pool, максимум MAX_PARALLEL_ANALYSES; насыщение даёт 429. Компоненты должны безопасно разделять неизменяемые ресурсы между запросами, изменять только факты текущего запроса.

Используйте HTTP-схемы из app/schemas.py и структуры из app/catalog.py. Код кандидата — исходный КТРУ, property.id — исходный code характеристики внутри позиции. Один code/название в разных позициях не доказывает одинаковую семантику. semantic_key/aliases требуют проверки. Category.id должна совпадать с ID из snapshot и строками индекса; UUID или вторую систему ID без адаптера не вводите. Backend отклоняет чужие ID от компонентов безопасным 503. Fact.id/span/raw нужны для исправления и происхождения; не теряйте отрицания, границы и исходные фрагменты. Исправления фактов требуют пользователя. Не считайте rank_score вероятностью.

## Ожидаемые артефакты

CATALOG_DIR содержит существующие catalog.json, manifest.json, vectors.npy, tokens.json; MODEL_DIR — локальные закреплённые веса. Формат и проверки уже реализованы в catalog.load_snapshot и prepare: одна строка матрицы 384 float на Category в порядке catalog.categories, нормированные векторы, BM25 tokens в том же порядке, SHA всех файлов, analyzer_version, catalog_version, model_id и 40-символьная model_revision. Запросы ничего не скачивают и не индексируют.

Поставьте нормализованный Catalog с данными адаптера clean_supply.load_clean_supply(...).catalog, дополненный проверенной картой типов. Сохраните все позиции/метаданные/единицы/unsupported; catalog_version оставьте версией адаптера. Смена карты типов отражается в checksum catalog.json/manifest; выдавайте новый комплект индексов. Готовый manifest имеет приоритет над сырым адаптером, но при наличии CLEAN_SUPPLY_DIR версия должна совпадать с текущими шестью файлами поставки. Неверные SHA/версия → resources_invalid, недоступные веса → model_unavailable. В API не выполняется подготовка артефактов; используйте уже существующий offline-процесс команды, второго индексатора нет.

Полнота Category.position_codes, link_verified, coverage_verified и evidence должна доказываться данными, включая раскрытие всех потомков parent_code. Эвристические category из архива не являются этой картой. Сейчас её и индексов clean (5) нет: загружены все 60 087 позиций, но готовых областей анализа 0. Backend не предлагает выдуманных типов и не подменяет отсутствие поиска ручным успешным выбором.

## Границы ответственности

Backend валидирует полный запрос заново, раскрывает конечную область до UI-лимита, вызывает компоненты, проверяет обязательные защиты и возвращает версии/статус. Подтверждение пользователя не отменяет source_issues, неподдерживаемый kind/type, пустые правила/интервалы, недостаточное покрытие и ошибки фактов. kind=3 пока unknown: поддержку множественного выбора согласуйте отдельным изменением checker и регрессиями, а не выставлением verified. Даже единственный совместимый кандидат требует подтверждённого типа и confirmed_ktru_code. Клиентские verification и normalized_value запрещены.

ML-команде осталось предоставить подтверждённую карту типов/полноту областей и согласованные готовые индексы. Базовые extractor/checker/questions доступны через адаптеры; их будущие реализации подключаются через эти порты. Frontend получает прежний полный POST-ответ; дополнительные признаки catalog/status описаны в API.md.
