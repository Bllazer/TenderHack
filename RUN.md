# Запуск проекта локально

Самый короткий путь — скрипты в корне репозитория:

```powershell
.\start.ps1              # API :8000 + интерфейс :5173   → http://127.0.0.1:5173
.\start.ps1 -Preview     # API :8000 + собранная версия :8080
.\start.ps1 -BackendOnly # только API
.\start.ps1 -Gpu         # считать эмбеддинги на GPU (по умолчанию CPU — стабильнее)
.\stop.ps1               # остановить всё, что подняли
```

`start.ps1` сам ставит зависимости фронтенда при первом запуске, ждёт готовности API и интерфейса,
складывает логи и pid-файлы в `.run/`. Если `python`/`pnpm` не в PATH, их пути можно задать
переменными `PYTHON_BIN`, `NODE_BIN`, `PNPM_BIN`. Ниже — то же самое руками.


Кратко: **два процесса** — бекенд (FastAPI + поиск, порт 8000) и фронтенд (Vite/React, порт 5173).
Фронтенд проксирует `/api` на бекенд сам (см. `frontend/vite.config.ts`), поэтому достаточно открыть
http://127.0.0.1:5173.

Данные и модели лежат в репозитории: `backend/resources/catalog-build` (снимок каталога КТРУ)
и `backend/resources/model` (эмбеддер E5). Сеть при запуске не нужна.

## Вариант 1 — быстрый (то, как запускается здесь)

Бекенд (из папки `backend`):

```powershell
$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
$env:CUDA_VISIBLE_DEVICES="-1"        # считаем на CPU: стабильно и без сюрпризов с драйвером
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Фронтенд (из папки `frontend`):

```powershell
pnpm install        # один раз
pnpm dev --host 127.0.0.1
```

Открыть: **http://127.0.0.1:5173** — это рабочее приложение (ввод описания, вопросы, карточки КТРУ, CSV).

## Вариант 2 — как в поставке (сборка + статика)

```powershell
# бекенд — как выше
cd frontend
pnpm install
pnpm build                       # tsc -b && vite build → frontend/dist
pnpm preview --host 127.0.0.1 --port 8080
```

`pnpm preview` проксирует `/api` так же, как dev-сервер (см. `vite.config.ts`), поэтому открывается
http://127.0.0.1:8080. Для «настоящей» раздачи есть `frontend/nginx.conf`
(там настроены `/api` → `127.0.0.1:8000`, SPA-fallback и `client_max_body_size 4m` под CSV до 2 МБ):

```powershell
# если nginx установлен
nginx -c <путь>\frontend\nginx.conf
```

## Проверка, что всё живо

```powershell
curl http://127.0.0.1:8000/api/health/ready      # {"status":"ok","catalog_version":"...","engine_version":"..."}
curl http://127.0.0.1:5173/                      # 200 — интерфейс
```

Быстрый сценарий в интерфейсе: «молоток» → подтвердить тип «Молоток» → 11 кодов → вопрос «Тип» → код.
Отказ: «панамка» → «нет похожих товаров» (не выдумывает тип).

## Тесты

```powershell
cd backend
python -m pytest -q                      # быстрый набор: 650 passed, 62 skipped, 1 xfailed
python -X utf8 tests/user_cases.py run --base http://127.0.0.1:8000    # 31 сценарий на живом API
python -X utf8 tests/batch_check.py --base http://127.0.0.1:8000       # пакетный режим и CSV
python -X utf8 tests/csv_input_check.py                                # разбор CSV без сервера

# с реальными весами e5 и полной поставкой
$env:RUN_E5_TESTS="1"; $env:RUN_CLEAN_SUPPLY_TESTS="1"; $env:CLEAN_SUPPLY_DIR="C:\hack\data\ktru_clean"
python -m pytest -q
```

Фронтенд: `pnpm build` (типы + сборка), `pnpm lint`, `pnpm test:e2e` (Playwright; нужны установленные браузеры).

## Если что-то не так

| Симптом | Причина и лечение |
|---|---|
| `503 analysis_unavailable`, в логе `CUDA error: unknown error` | слетел GPU-контекст (драйвер, а не код). Запустите бекенд с `CUDA_VISIBLE_DEVICES=-1` — заработает на CPU |
| `Port 8000/5173 already in use` | старый процесс: `Get-NetTCPConnection -LocalPort 8000 -State Listen` → `Stop-Process -Id <pid> -Force` |
| Долгий первый ответ | модель e5 грузится в память при старте, первый запрос после запуска медленнее |
| CSV «0 строк» или искажённые описания | проверьте кодировку файла (UTF-8/UTF-16/cp1251) и заголовок; при спорном разделителе передайте `csv_delimiter` (`;` `,` таб `|`) |
| Файл больше 256 КБ не проходит | это лимит nginx: в `frontend/nginx.conf` стоит `client_max_body_size 4m` (до 2 МБ исходника в base64) |

Порты: **8000** — API (документация: http://127.0.0.1:8000/api/docs, схема: `/api/openapi.json`), **5173** — интерфейс разработки, **8080** — просмотр собранной версии (`pnpm preview`).

Документация API и поведения: `backend/docs/API.md`, ограничения значений — `backend/docs/VALUE_LIMITS.md`,
разбор замечаний — `backend/docs/OWNER_FINDINGS.md`, отчёт по последней итерации — `backend/docs/REPORT_GROUPS_1_6.md`.
