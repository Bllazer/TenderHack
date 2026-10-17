# Запуск проекта локально

Самый короткий путь — скрипты в корне репозитория:

```powershell
.\start.ps1              # API :8000 + интерфейс :5173   → http://127.0.0.1:5173
.\start.ps1 -Preview     # API :8000 + собранная версия :8080
.\start.ps1 -BackendOnly # только API
.\start.ps1 -Gpu         # не скрывать GPU (torch в .venv всё равно CPU-сборка)
.\start.ps1 -Reinstall   # пересобрать backend\.venv и переустановить фронтенд
.\start.ps1 -NoBrowser   # не открывать интерфейс в браузере автоматически
.\stop.ps1               # остановить всё, что подняли
```

Обычный запуск поднимает dev-сервер на **5173**. Порт **8080** — только режим
`-Preview` (собранная версия); без него на 8080 ничего не слушает.
`start.ps1` открывает браузер сам; отключить — `-NoBrowser`.

Можно запускать двойным кликом по `start.cmd` — он сам вызовет `start.ps1` с
`-ExecutionPolicy Bypass` и предпочтёт `pwsh`, если он установлен.

При первом запуске `start.ps1`:

- создаёт `backend\.venv` и ставит туда зависимости из `backend\pyproject.toml`
  через `uv sync --frozen` (сам `uv` скачивается в `.tools\uv`, если его нет);
- если `node`/`pnpm` не найдены, скачивает Node.js LTS и pnpm в `.tools\`;
- ставит зависимости фронтенда при отсутствии `frontend\node_modules`;
- ждёт готовности API и интерфейса, складывает логи и pid-файлы в `.run\`.

Для первого запуска нужен интернет (зависимости и Node.js). Дальше всё работает
офлайн. Если всё уже установлено и нужно запустить без сети — `-SkipInstall`.
Пути к своим `python`/`node`/`pnpm` можно задать переменными `PYTHON_BIN`,
`NODE_BIN`, `PNPM_BIN`.

Ниже — то же самое руками.


Кратко: **два процесса** — бекенд (FastAPI + поиск, порт 8000) и фронтенд (Vite/React, порт 5173).
Фронтенд проксирует `/api` на бекенд сам (см. `frontend/vite.config.ts`), поэтому достаточно открыть
http://127.0.0.1:5173.

Данные и модели лежат локально: `backend/resources/catalog-build` (снимок каталога КТРУ)
и `backend/resources/model` (эмбеддер E5). После установки зависимостей сеть при запуске не нужна.

## Вариант 1 — быстрый (то, как запускается здесь)

Бекенд (из папки `backend`). Нужен Python 3.12+; зависимости ставятся в локальное
окружение `.venv`, чтобы не зависеть от глобального Python:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.6,<3"
.\.venv\Scripts\python.exe -m pip install -e ".[search]"

$env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
$env:CUDA_VISIBLE_DEVICES="-1"        # считаем на CPU: стабильно и без сюрпризов с драйвером
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Если установлен `uv`, короче: `uv sync --frozen --extra search --no-dev`, затем
`uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8000`.

Фронтенд (из папки `frontend`, нужен Node.js 22.12+ и pnpm):

```powershell
pnpm install --frozen-lockfile
pnpm dev --host 127.0.0.1
```

Открыть: **http://127.0.0.1:5173** — это рабочее приложение (ввод описания, вопросы, карточки КТРУ, CSV).

## Вариант 2 — как в поставке (сборка + статика)

```powershell
# бекенд — как выше
cd frontend
pnpm install --frozen-lockfile
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
.\.venv\Scripts\python.exe -m pytest -q                      # быстрый набор: 650 passed, 62 skipped, 1 xfailed
.\.venv\Scripts\python.exe -X utf8 tests/user_cases.py run --base http://127.0.0.1:8000    # 31 сценарий на живом API
.\.venv\Scripts\python.exe -X utf8 tests/batch_check.py --base http://127.0.0.1:8000       # пакетный режим и CSV
.\.venv\Scripts\python.exe -X utf8 tests/csv_input_check.py                                # разбор CSV без сервера

# с реальными весами e5 и полной поставкой
$env:RUN_E5_TESTS="1"; $env:RUN_CLEAN_SUPPLY_TESTS="1"; $env:CLEAN_SUPPLY_DIR="C:\hack\data\ktru_clean"
.\.venv\Scripts\python.exe -m pytest -q
```

Фронтенд: `pnpm build` (типы + сборка), `pnpm lint`, `pnpm test:e2e` (Playwright; нужны установленные браузеры).

## Если что-то не так

| Симптом | Причина и лечение |
|---|---|
| `Не найден Python 3.12+` | установите Python 3.12 и перезапустите; свой путь задайте через `$env:PYTHON_BIN` |
| Не скачивается `uv`/Node.js/pnpm | нужен интернет на первом запуске; при закрытой сети поставьте Node.js 22.12+ и pnpm вручную или укажите `NODE_BIN`/`PNPM_BIN` |
| `start.ps1 нельзя загрузить, выполнение сценариев отключено` | запускайте `start.cmd` или `powershell -ExecutionPolicy Bypass -File .\start.ps1` |
| `503 analysis_unavailable`, в логе `CUDA error: unknown error` | слетел GPU-контекст (драйвер, а не код). Запустите бекенд с `CUDA_VISIBLE_DEVICES=-1` — заработает на CPU |
| `Port 8000/5173 already in use` | старый процесс: `.\stop.ps1`, либо `Get-NetTCPConnection -LocalPort 8000 -State Listen` → `Stop-Process -Id <pid> -Force` |
| `Backend-зависимости не установились` | посмотрите ошибку выше (обычно сеть/прокси); затем `.\start.ps1 -Reinstall` |
| Долгий первый ответ | модель e5 грузится в память при старте, первый запрос после запуска медленнее |
| CSV «0 строк» или искажённые описания | проверьте кодировку файла (UTF-8/UTF-16/cp1251) и заголовок; при спорном разделителе передайте `csv_delimiter` (`;` `,` таб `|`) |
| Файл больше 256 КБ не проходит | это лимит nginx: в `frontend/nginx.conf` стоит `client_max_body_size 4m` (до 2 МБ исходника в base64) |

Порты: **8000** — API (документация: http://127.0.0.1:8000/api/docs, схема: `/api/openapi.json`), **5173** — интерфейс разработки, **8080** — просмотр собранной версии (`pnpm preview`).

Документация API и поведения: `backend/docs/API.md`, ограничения значений — `backend/docs/VALUE_LIMITS.md`,
разбор замечаний — `backend/docs/OWNER_FINDINGS.md`, отчёт по последней итерации — `backend/docs/REPORT_GROUPS_1_6.md`.
