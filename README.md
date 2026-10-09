# TenderHack

Репозиторий команды хакатона 9–11 октября 2026.

- `frontend/` — React + TypeScript + Vite.
- `backend/` — FastAPI, PostgreSQL, Alembic, базовый алгоритм КТРУ.
- [`backend/docs/API.md`](backend/docs/API.md) — действующий контракт для фронтенда. Обновлять в одном коммите с изменением API.
- [`backend/docs/RESEARCH.md`](backend/docs/RESEARCH.md) — постановка задачи, источники и открытые вопросы к датасету.

## Запуск API

```sh
docker compose up --build
```

Swagger: http://localhost:8000/api/docs. Проверка готовности: http://localhost:8000/api/health/ready. Каталог изначально пуст; API возвращает `needs_data`, пока не загружен набор организаторов. Для отдельного синтетического примера и локальной разработки см. [backend/README.md](backend/README.md).

## Запуск фронтенда

Нужен Node.js 22.12+ (или более новый поддерживаемый LTS) и pnpm.

```sh
npm install --global pnpm@11
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Открыть http://localhost:5173. Vite перенаправляет `/api/*` на FastAPI. Подробнее о структуре и проверках — в [frontend/README.md](frontend/README.md).
