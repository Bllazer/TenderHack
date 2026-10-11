import logging
from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.analysis import InvalidConfirmation, analyze
from app.batch import batch_counts, summarize
from app.catalog import Catalog, load_snapshot, snapshot
from app.config import settings
from app.csv_input import CsvEncodingError, decode_base64, split_rows
from app.schemas import (AnalysisCreate, AnalysisOut, BatchCreate, BatchOut, BatchRow, CatalogStatus,
                         CsvRowsOut, ErrorResponse, Health, Ready, ENGINE_VERSION)
from app.search import E5Encoder, Search
from app.components import existing_components
from app.catalog_diagnostics import position_blockers
from app.schemas import CatalogPositionCard

logger = logging.getLogger(__name__)


def resources():
    try:
        if settings.clean_supply_dir and settings.clean_supply_dir.exists():
            from app.clean_supply import expected_supply_version, load_clean_supply
            if (settings.catalog_dir / "manifest.json").exists():
                data = load_snapshot(settings.catalog_dir)
                if data.catalog.catalog_version != expected_supply_version(settings.clean_supply_dir):
                    raise ValueError("Prepared snapshot does not match the clean supply")
            else:
                data = load_clean_supply(settings.clean_supply_dir)
        else:
            data = load_snapshot(settings.catalog_dir)
    except Exception:
        logger.exception("Catalog/index initialization failed")
        data = snapshot(Catalog(catalog_version="unavailable", source_description="", validation_report=""), problem="resources_invalid")
    search = None
    if data.vectors is not None:
        try:
            encoder = E5Encoder(settings.model_dir, data.model_revision)
            search = Search(data, encoder)
        except Exception:
            logger.exception("Local embedding model initialization failed")
            from dataclasses import replace
            data = replace(data, problem="model_unavailable")
            search = Search(data, None)
    return data, search


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.data, app.state.search = await run_in_threadpool(resources)
    app.state.slots = BoundedSemaphore(max(1, settings.max_parallel_analyses))
    app.state.components = await run_in_threadpool(existing_components, app.state.data)
    yield


app = FastAPI(title="TenderHack stateless API", version="0.2.0", docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[item.strip() for item in settings.cors_origins.split(",")],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/api/health", response_model=Health)
@app.get("/api/health/live", response_model=Health)
def live():
    return {"status": "ok"}


@app.get("/api/health/ready", response_model=Ready, responses={503: {"model": ErrorResponse}})
def ready():
    data = app.state.data
    missing = [*data.missing_resources, *app.state.components.missing]
    if data.problem or not app.state.search or missing:
        raise HTTPException(503, {"code": data.problem or (missing[0] + "_missing" if missing else "search_unavailable"),
                                  "catalog_version": data.catalog.catalog_version, "missing_resources": missing})
    return {"status": "ok", "catalog_version": data.catalog.catalog_version, "engine_version": ENGINE_VERSION}


@app.get("/api/v1/catalog/status", response_model=CatalogStatus)
def catalog_status():
    data = app.state.data
    return {"categories": len(data.categories), "positions": len(data.positions),
            "catalog_version": data.catalog.catalog_version, "ready": not data.problem and bool(app.state.search) and not app.state.components.missing,
            "reason": data.problem or (app.state.components.missing[0] + "_missing" if app.state.components.missing else None),
            "catalog_loaded": bool(data.positions),
            "missing_resources": list(data.missing_resources) + list(app.state.components.missing),
            "templates": sum(p.is_template is True for p in data.positions.values()),
            "final_positions": sum(p.is_template is False for p in data.positions.values())}


@app.get("/api/v1/catalog/positions/{code}", response_model=CatalogPositionCard,
         responses={404: {"model": ErrorResponse}})
def catalog_position(code: str):
    data = app.state.data
    position = data.positions.get(code)
    if position is None:
        raise HTTPException(404, {"code": "unknown_ktru_code", "field": "code"})
    return CatalogPositionCard(catalog_version=data.catalog.catalog_version, position=position,
                               blocking_reasons=position_blockers(position))



@app.get("/api/v1/catalog/categories/{category_id}/properties")
def category_properties(category_id: str):
    from app.questions import property_question_id
    data = app.state.data
    if category_id not in data.categories:
        raise HTTPException(404, {"code": "unknown_category"})
    positions, complete = data.pool(category_id)
    properties = tuple(prop for position in positions for prop in position.properties)
    context = getattr(app.state.components.extractor, "context", None)
    if context:
        properties, _, _ = context(category_id, properties)
    fields = {}
    for prop in properties:
        key = (prop.subject, prop.name)
        field = fields.setdefault(key, {"name": prop.name, "subject": prop.subject,
            "aliases": set(), "required": False, "multiple": False, "options": set(), "units": set(),
            "question_id": property_question_id(category_id, prop) if prop.source == "ktru" else None,
            "source": prop.source})
        field["required"] |= bool(prop.required)
        field["multiple"] |= prop.kind == "3"
        field["aliases"].update(prop.aliases)
        field["options"].update(a.value for a in prop.allowed if a.kind == "enum" and a.value)
        field["units"].update(a.unit for a in prop.allowed if a.unit)
    return {"category_id": category_id, "complete": complete, "catalog_version": data.catalog.catalog_version,
        "properties": [{**field, "aliases": sorted(field["aliases"]), "options": sorted(field["options"]),
                        "units": sorted(field["units"])} for field in sorted(fields.values(), key=lambda f: (not f["required"], f["name"]))]}

@app.post("/api/v1/analyses", response_model=AnalysisOut,
          responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
def create_analysis(body: AnalysisCreate):
    if not app.state.slots.acquire(blocking=False):
        raise HTTPException(429, {"code": "analysis_busy", "message": "Повторите запрос после завершения текущего анализа"})
    try:
        return analyze(app.state.data, app.state.search, body, components=app.state.components)
    except InvalidConfirmation as exc:
        raise HTTPException(422, {"code": exc.code, "field": exc.field,
                                  "message": "Подтверждение не соответствует текущей области и фактам"}) from exc
    except Exception as exc:
        logger.exception("Analysis failed")
        raise HTTPException(503, {"code": "analysis_unavailable", "message": "Не удалось выполнить проверку; повторите запрос"}) from exc
    finally:
        app.state.slots.release()


@app.post("/api/v1/analyses/csv", response_model=CsvRowsOut,
          responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
def parse_csv(body: BatchCreate):
    """Только разбор загруженного файла: строки для поштучного прогона.

    Ничего не анализирует и не занимает слот: клиент получает список строк и
    дальше отправляет каждую в обычный `/api/v1/analyses` — так строка CSV
    проходит тот же путь, что и ввод вручную.
    """
    try:
        raw = decode_base64(body.csv_base64) if body.csv_base64 is not None else body.csv.encode("utf-8")
    except ValueError as error:
        raise HTTPException(422, {"code": "csv_invalid", "field": "csv_base64",
                                  "message": str(error)}) from error
    try:
        parsed = split_rows(raw, max_rows=body.run_limit, skip_header=body.skip_header,
                            delimiter=body.csv_delimiter)
    except CsvEncodingError as error:
        raise HTTPException(422, {"code": error.code, "field": "csv",
                                  "message": str(error)}) from error
    if not parsed.rows:
        # Причина важнее общего текста: строка могла быть отклонена (например, слишком длинное
        # описание), и пользователю нужно знать, что именно не так.
        reason = (parsed.skipped[0]["reason"] if parsed.skipped else
                  parsed.notes[-1] if parsed.notes else "В файле не найдено ни одной строки с товаром")
        raise HTTPException(422, {"code": "csv_empty", "field": "csv", "message": reason})
    return CsvRowsOut(catalog_version=app.state.data.catalog.catalog_version, filename=body.filename,
                      encoding=parsed.encoding, delimiter=parsed.delimiter,
                      header_skipped=parsed.header_skipped, rows_total=len(parsed.rows),
                      rows=parsed.rows, rows_skipped=parsed.skipped, notes=parsed.notes,
                      truncated=parsed.truncated)


@app.post("/api/v1/analyses/batch", response_model=BatchOut,
          responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
def create_batch(body: BatchCreate):
    """Загруженный CSV: каждая строка файла разбирается как обычная строка ввода.

    Формат файла не задан, поэтому строка собирается из всех непустых ячеек через
    `"; "` — тот же текст, который пользователь ввёл бы руками. Строки
    обрабатываются последовательно в одном слоте, чтобы пакет не вытеснял
    одиночные запросы; по каждой строке возвращается компактная сводка.
    """
    try:
        raw = decode_base64(body.csv_base64) if body.csv_base64 is not None else body.csv.encode("utf-8")
    except ValueError as error:
        raise HTTPException(422, {"code": "csv_invalid", "field": "csv_base64",
                                  "message": str(error)}) from error
    try:
        parsed = split_rows(raw, max_rows=body.run_limit, skip_header=body.skip_header,
                            delimiter=body.csv_delimiter)
    except CsvEncodingError as error:
        raise HTTPException(422, {"code": error.code, "field": "csv",
                                  "message": str(error)}) from error
    if not parsed.rows:
        # Причина важнее общего текста: строка могла быть отклонена (например, слишком длинное
        # описание), и пользователю нужно знать, что именно не так.
        reason = (parsed.skipped[0]["reason"] if parsed.skipped else
                  parsed.notes[-1] if parsed.notes else "В файле не найдено ни одной строки с товаром")
        raise HTTPException(422, {"code": "csv_empty", "field": "csv", "message": reason})
    if not app.state.slots.acquire(blocking=False):
        raise HTTPException(429, {"code": "analysis_busy",
                                  "message": "Повторите запрос после завершения текущего анализа"})
    try:
        rows = []
        for item in parsed.rows:
            base = {"line": item["line"], "description": item["description"]}
            try:
                single = AnalysisCreate(description=item["description"], candidate_limit=body.candidate_limit)
                result = analyze(app.state.data, app.state.search, single, components=app.state.components)
                rows.append(BatchRow(**base, **summarize(result)))
            except InvalidConfirmation as exc:
                rows.append(BatchRow(**base, status="error", error=exc.code))
            except Exception as exc:
                logger.exception("Batch row failed on line %s", item["line"])
                rows.append(BatchRow(**base, status="error", error=type(exc).__name__))
        return BatchOut(catalog_version=app.state.data.catalog.catalog_version, filename=body.filename,
                        encoding=parsed.encoding, delimiter=parsed.delimiter,
                        header_skipped=parsed.header_skipped, rows_total=len(parsed.rows),
                        rows_skipped=parsed.skipped, notes=parsed.notes, truncated=parsed.truncated,
                        counts=batch_counts([row.model_dump() for row in rows]), rows=rows)
    finally:
        app.state.slots.release()
