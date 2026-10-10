# Установка модели и индексов

Для backend-разработчика и для работы с алгоритмами. **Обеим ролям нужны одни и
те же два артефакта** — разница только в том, что backend запускает сервис, а
алгоритмам нужен поиск для экспериментов.

## Что должно оказаться на диске

| Путь (от `backend/`) | Размер | Что это | В git |
|---|---|---|---|
| `resources/model/` | **470 МБ** | веса модели | нет |
| `resources/catalog-build/` | **738 МБ** | карта типов и индекс | нет |

Обе папки в `.gitignore`. В репозитории только пустая заглушка
`resources/model/.gitkeep`.

## Требования

Python **3.12+**. Зависимости объявлены в `pyproject.toml`:

```toml
dependencies = ["fastapi", "uvicorn[standard]", "pydantic-settings", "numpy", "pymorphy3"]
[project.optional-dependencies]
search = ["torch>=2.6,<3", "transformers>=4.57,<5", "huggingface-hub>=0.36,<1"]
dev    = ["pytest>=8,<10", "httpx>=0.27,<1"]
```

Модель требует extra **`search`**, проверка — extra **`dev`**.

---

## Шаг 1. Зависимости

**Вариант A — uv, как в Dockerfile** (`uv==0.12.24`):

```sh
cd backend
uv sync --frozen --extra search --extra dev
```

> ⚠️ torch встанет **CPU-версией**: в `pyproject.toml` он привязан к индексу
> `pytorch-cpu` через `[tool.uv.sources]`. Для контейнера это правильный выбор.
> Если нужна видеокарта — см. шаг 2.

**Вариант B — pip:**

```sh
cd backend
pip install -e ".[search,dev]"
```

## Шаг 2. GPU — опционально

Код сам выбирает устройство: при наличии CUDA берёт её, иначе работает на CPU.
Без этого шага **ничего не сломается**, просто индексация и запросы медленнее.

```sh
uv pip install "torch>=2.6,<3" --index-url https://download.pytorch.org/whl/cu124
```

Проверка:

```sh
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"
```

Ожидается примерно: `2.6.0+cu124 True NVIDIA GeForce RTX 4050 Laptop GPU`

Замеры на RTX 4050: индексация **92 с** против 732 с на CPU, запрос **p50 29–44 мс**.

## Шаг 3. Веса модели

Из каталога `backend/`:

```sh
python -m app.prepare download-model resources/model \
    --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3
```

- **Ревизия обязательна и должна быть ровно 40 hex-символов.** Иначе команда
  падает с `ValueError: Pass the exact 40-character Hugging Face commit SHA` —
  так исключена случайная загрузка «примерно той же» версии.
- Скачивается ~470 МБ, затем создаётся `model.meta.json` с SHA256 каждого файла.

После команды в папке должно быть:

| Файл | Размер |
|---|---|
| `model.safetensors` | 448,8 МБ |
| `tokenizer.json` | 16,3 МБ |
| `sentencepiece.bpe.model` | 4,8 МБ |
| `config.json`, `tokenizer_config.json`, `special_tokens_map.json` | мелочь |
| `model.meta.json` | ревизия и контрольные суммы |

## Шаг 4. Индекс

Нужен, если артефактов ещё нет. Поставка — распакованный `ktru_clean`
(две идентичные копии: `C:\hack\data\ktru_clean` и копия в OneDrive).

```sh
python -m app.category_map "<путь к ktru_clean>" resources/catalog-build \
    --model-dir resources/model --compact-raw
python -m app.prepare index resources/catalog-build --model-dir resources/model
```

Ожидаемый вывод первой команды:

```
  позиций в поставке: 60087
  categories: 14550
  terminal_positions_covered: 54190
  terminal_positions_missing_count: 0
```

Вторая занимает ~90 с на GPU. Появится `vectors.npy` (21 МБ), `tokens.json`
(11 МБ), `manifest.json`. `manifest.json` пишется последним: неполный набор
артефактов readiness не примет.

## Шаг 5. Проверка

```sh
python tests/verify_real_supply.py     # ожидается: 7 из 8, провал на перефразировке
python tests/ask.py                    # ручной поиск, диалог без перезагрузки
```

`ask.py` без аргументов грузит каталог один раз и дальше отвечает за 30–190 мс.

---

## Docker

Веса монтируются **с хоста**, поэтому скачать их надо **на хосте, до запуска**:

```sh
# на хосте, из backend/
python -m app.prepare download-model resources/model --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3

# из корня репозитория
docker compose up --build -d
curl http://localhost:8000/api/health/ready
```

Монтирование: `./backend/resources/model:/srv/resources/model:ro` — контейнер
видит хостовую папку только на чтение. **Индекс тоже нужен на хосте**:
`CATALOG_DIR` в Compose указывает на `/srv/resources/catalog-build`, то есть на
`backend/resources/catalog-build`. Поставка монтируется из `./ktru_clean` в
`/srv/clean-supply`; подготовленный каталог сверяется с её версией. Перед первым
запуском выполните обе команды шага 4, если готового валидного набора ещё нет.
Готовый набор повторно не индексируйте.

Другие пути на хосте задаются через `TENDERHACK_CATALOG_PATH`,
`TENDERHACK_MODEL_PATH`, `TENDERHACK_CLEAN_SUPPLY_PATH` в окружении или локальном
корневом `.env`. Внутренние пути контейнера остаются неизменными. Пример из корня:

```powershell
$env:TENDERHACK_CATALOG_PATH='./backend/resources/catalog-build'
$env:TENDERHACK_MODEL_PATH='./backend/resources/model'
$env:TENDERHACK_CLEAN_SUPPLY_PATH='./ktru_clean'
docker compose config --quiet
docker compose up --build -d
```

Открыть frontend: http://localhost:8080. Проверить прокси и ресурсы:
http://localhost:8080/api/health/ready и http://localhost:8080/api/v1/catalog/status.
Остановка из корня: `docker compose stop`; повторный запуск: `docker compose up -d`.
Ресурсы исключены из контекста Docker-сборки и не включаются в образ.

Внутри контейнера torch **CPU-версия** (`uv sync --frozen --extra search` берёт
индекс `pytorch-cpu`). Для сервиса это нормально; для массовой переиндексации
лучше делать это на хосте с GPU.

## Если интернета нет

Скопировать **две папки целиком**: `resources/model/` и `resources/catalog-build/`.
Ничего дополнительно настраивать не нужно — при загрузке сверяются SHA256 всех
файлов из `model.meta.json` и `manifest.json`, поэтому повреждённая или чужая
копия не пройдёт молча.

---

## Что может пойти не так

| Сообщение | Причина | Что делать |
|---|---|---|
| `Pass the exact 40-character Hugging Face commit SHA` | ревизия не 40 hex | взять из этого документа |
| `Model/index revision mismatch` | веса и индекс от разных ревизий | скачать веса этой ревизии **или** пересобрать индекс |
| `Model checksum mismatch` | файл повреждён, удалён или подменён | удалить папку и скачать заново |
| `Model metadata does not cover necessary weights/tokenizer` | `model.meta.json` неполный | перекачать веса |
| readiness 503 `search_index_missing` | индекс не собран | шаг 4 |
| readiness 503 `model_unavailable` | веса не найдены или не проходят проверку | шаг 3 |
| `Catalog search records must fit in 512 tokens` | индекс собирается другим токенизатором | использовать модель закреплённой ревизии |
| 404 на `/live`, `/ready` | у маршрутов есть префикс | правильные пути: `/api/health/live`, `/api/health/ready` |

> ⚠️ **Про повторное скачивание.** `download_model` хеширует **все** файлы,
> лежащие в папке, а не только скачанные. Если после неудачной попытки там остался
> мусор, он попадёт в `model.meta.json` и будет проверяться при каждом запуске.
> Перед повторным скачиванием папку лучше очистить, оставив только `.gitkeep`.

## Полезно знать

- **Старт сервиса ~17 секунд**: 10 с на чтение и валидацию каталога (706 МБ),
  6 с на загрузку весов. Это не зависание; ждать надо `/api/health/ready`.
- **Переменные окружения** для локального запуска:
  `CATALOG_DIR=resources/catalog-build`, `MODEL_DIR=resources/model`,
  `CLEAN_SUPPLY_DIR=<путь к ktru_clean>`. По умолчанию `clean_supply_dir`
  указывает на `../ktru_clean`, а в репозитории такой папки нет.
- **Ревизия модели должна совпадать с индексом.** Если перекачать веса другой
  ревизии, индекс придётся пересобрать — иначе `E5Encoder` откажется стартовать.
