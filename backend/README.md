# Stateless backend

Python 3.12 / FastAPI, один процесс API. Нет PostgreSQL, SQLAlchemy, Alembic, Redis, файловых сессий или сохранения анализов. Изменение факта запускает полную проверку полного состояния запроса. Контракт: [API.md](docs/API.md).

## Запуск

Compose находится в корне репозитория. Из корня:

```sh
docker compose config --quiet
docker compose up --build -d
docker compose logs api
```

До запуска подготовьте ресурсы по [MODEL_SETUP.md](docs/MODEL_SETUP.md): очищенная
поставка в `ktru_clean/` от корня, веса в `backend/resources/model/`, согласованный
набор `catalog.json`, `manifest.json`, `vectors.npy`, `tokens.json` в
`backend/resources/catalog-build/`. Пустой каталог не является готовым сервисом.
Compose монтирует эти три каталога только для чтения. Другие расположения задаются
переменными `TENDERHACK_CLEAN_SUPPLY_PATH`, `TENDERHACK_MODEL_PATH`,
`TENDERHACK_CATALOG_PATH` в окружении или корневом локальном `.env`; личные пути
в git не добавляйте. В контейнере пути соответственно `/srv/clean-supply`,
`/srv/resources/model`, `/srv/resources/catalog-build`. Для локального API
используются `CLEAN_SUPPLY_DIR`, `MODEL_DIR`, `CATALOG_DIR` из `.env.example`.
Проверка готовности: `http://localhost:8080/api/health/ready` и
`http://localhost:8080/api/v1/catalog/status`. Остановка из корня:
`docker compose stop`; повторный запуск: `docker compose up -d`.

Из `backend/` эквивалентная команда: `docker compose -f ../docker-compose.yml up --build -d`. Frontend — http://localhost:8080, документация — http://localhost:8000/api/docs. API слушает один worker; синхронная работа выполняется в thread pool, одновременно допускаются два анализа, остальные получают 429.

Распакованная поставка `../ktru_clean/` подключается через `CLEAN_SUPPLY_DIR` (в Compose `/srv/clean-supply`, read-only). Загружены 60 087 действующих позиций: 5 897 шаблонов, 54 190 конечных, 234 363 характеристики. Источники не изменяются. Liveness возвращает 200; без подготовленного индекса readiness — 503 `search_index_missing`, анализ — 200 `needs_data`. При отсутствии поставки используется прежний нормализованный каталог; пустой каталог даёт `catalog_empty`. Тестовые фикстуры никогда не загружаются рабочим приложением. Миграций и импорта в БД нет.

Карта типов и индексы собираются офлайн в игнорируемую папку `resources/catalog-build/` (команды — в [docs/ML_DELIVERY.md](docs/ML_DELIVERY.md), §8): 14 550 областей анализа, покрытие всех 54 190 конечных позиций. Артефактов в репозитории нет, поэтому readiness без сборки — 503.

**Что ещё требует правок:** [docs/HANDOVER_BACKEND.md](docs/HANDOVER_BACKEND.md) — порог значимости в поиске и расширение таблицы единиц. Неверные коды `OKEI_SUPPORTED` (`050`, `051`, `271`, `274`) уже исправлены.

## Локально

Из `backend/` (нужен uv):

```sh
uv sync --frozen --extra search --extra dev
uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
uv run --no-sync pytest -q
```

В другом терминале из `frontend/`: `pnpm install --frozen-lockfile`, `pnpm dev`. `/api` сохраняется; `API_PROXY_TARGET` настраивает адрес backend. Настройки backend описаны в `.env.example`.

## Локальные ресурсы

Поставка и ограничения описаны в [CATALOG.md](docs/CATALOG.md), порты для ML-команды — в [ML_INTEGRATION.md](docs/ML_INTEGRATION.md). Существующий offline-адаптер `app.category_map` строит области по связям исходных позиций, сохраняя метаданные и происхождение. Он сохраняет неподдерживаемые правила; kind=3 пока unknown. Семь source_issues и позиции без характеристик не допускают resolved. Готовый согласованный manifest в CATALOG_DIR имеет приоритет, его catalog_version сверяется с исходной поставкой.

Каталог, индексы и модель загружаются один раз в lifespan. После смены ресурсов нужен перезапуск. Snapshot неизменяемый; файлы в Docker монтируются только для чтения. Запрос не скачивает модель и не пересчитывает векторы каталога.

Подготовка до запуска, из `backend/`:

```sh
uv run --no-sync python -m app.prepare download-model resources/model --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3
# Только если нет актуального согласованного набора, после проверки поставки:
uv run --no-sync python -m app.category_map ../ktru_clean resources/catalog-build --model-dir resources/model --compact-raw
uv run --no-sync python -m app.prepare index resources/catalog-build --model-dir resources/model
```

Модель — локальная `intfloat/multilingual-e5-small`, masked mean pooling, нормализация, префиксы `query: ` / `passage: `. Индекс — NumPy 384 измерения и заранее лемматизированные документы BM25; объединение RRF, rank не вероятность. SHA файлов, версия анализатора, версия каталога и revision модели сверяются при запуске. Веса не коммитятся, но должны присутствовать на хосте до анализа непустого каталога. Docker использует CPU torch и offline-режим.

Настоящий локальный E5 проверяется отдельно (модель уже должна быть скачана):

```sh
# PowerShell из backend/
$env:RUN_E5_TESTS='1'
$env:RUN_CLEAN_SUPPLY_TESTS='1'
$env:CLEAN_SUPPLY_DIR=(Resolve-Path ../ktru_clean).Path
uv run --no-sync pytest -q
```

Без RUN_E5_TESTS тест реальной модели явно пропускается; без RUN_CLEAN_SUPPLY_TESTS пропускается загрузка всего локального корпуса. Остальные тесты используют синтетическую вертикаль и отдельно реальные записи clean (5) из tests/fixtures/clean5-real.json. Read-only HTTP smoke поднятого Compose (из backend/): `uv run --no-sync python tests/docker_smoke.py`; проверяются порт API и /api через nginx. Браузерные тесты остаются ответственностью frontend-команды.

## Реализованная ветка и ограничения

После независимого ревью e382611: C/D и structured подключены через adapter; C используется как дополнительный запрет успешного результата текущей строгой проверки. Предлагаемая правка значимого имени требует решения пользователя (kind=correction), иначе needs_input. Отчёт проверок: [REVIEW_e382611_FIXES.md](docs/REVIEW_e382611_FIXES.md). Пакет algorithms и его unit data включены в wheel и Docker. Новые веса, серверное хранение и frontend-изменения не добавлены.

- `extraction.py`: исходные фрагменты/смещения, пары ключ–значение, проверенные словари категории, простые свободные фразы, отрицания, числа, минусы, сравнения, составные единицы, `unparsed`.
- `search.py`: морфологический BM25 + локальный E5, RRF и точный поиск NumPy; исходный текст участвует независимо от извлечения.
- `mapping.py`: сопоставление в контексте позиции и объекта (товар/упаковка), КТРУ перед проверенными ПП/СПГЗ и алиасами. Неизвестные дополнительные сведения сохраняются.
- `checks.py`: весь пул конечных позиций, типы, размерности, допустимые элементы, простые интервалы/OR и строгие границы; `compatible/incompatible/unknown`, причины и value_code. Пустые/непроверенные правила не доказывают совместимость.
- `questions.py`: конфликт → категория → разделитель → обязательное поле; «не знаю» сохраняет неопределённость. Одинаковые подтверждённые ограничения прекращают вопросы по значениям и дают `ambiguous`.
- `analysis.py`: оркестрация, полная повторная проверка, ручное подтверждение категории и кода без обхода несовместимости. Диагностика из шаблонов, без генеративной модели.

На локальном запуске проверен полный сценарий HTTP и браузера для **трёх реальных типов**: строительный битум, огнеупорная глина и шпажки для шашлыка. Поисковый snapshot содержит 14 550 областей и все 54 190 конечных позиций; это покрытие графа, а не гарантия поддержки всех ограничений. Версии, SHA, примеры, фактические результаты и ограничения: [LOCAL_RUN_2026-10-10.md](docs/LOCAL_RUN_2026-10-10.md). Существующие ML/алгоритмы не переписывались; источники и тяжёлые артефакты не добавлены в git.

Поддерживаются только представленные в схеме простые числовые/качественные/логические ограничения и ограниченный справочник единиц. Неизвестные единицы, сложные условные правила, неподдерживаемые конструкции и недостаточное покрытие блокируют завершение. Нет обучения, резервной LLM, похожих альтернатив или гарантии качества по всему каталогу. Исторический `docs/RESEARCH.md` не является спецификацией текущей архитектуры.
