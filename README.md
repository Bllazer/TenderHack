# TenderHack

React/Vite и stateless Python/FastAPI: описание → извлечение → поиск категории → уточнение → проверка конечных позиций → ручное подтверждение кода. Данные формы живут в браузере; каждый запрос передаёт полное текущее состояние. Базы данных и серверного хранения анализов нет.

```sh
# Из корня репозитория
docker compose up --build -d
```

Frontend: http://localhost:8080, API: http://localhost:8000/api/docs. Nginx и Vite сохраняют `/api`.

Очищенные данные ещё не предоставлены. Рабочий каталог намеренно пуст: API отвечает `needs_data`, readiness — HTTP 503. Синтетические данные используются только тестами; демонстрационные коды не выдаются за КТРУ.

- [Запуск, ресурсы и проверки backend](backend/README.md)
- [Контракт API и сценарий frontend](backend/docs/API.md)
- [Формат проверенной поставки](backend/docs/CATALOG.md)
- [Frontend](frontend/README.md)

Для разработки frontend из `frontend/`: `pnpm install --frozen-lockfile`, `pnpm dev`, `pnpm build`, `pnpm lint`. Backend запускается отдельно; Vite проксирует API на порт 8000.
