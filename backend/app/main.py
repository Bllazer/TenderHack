import logging
from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.analysis import InvalidConfirmation, analyze
from app.catalog import Catalog, load_snapshot, snapshot
from app.config import settings
from app.schemas import AnalysisCreate, AnalysisOut, CatalogStatus, ErrorResponse, Health, Ready, ENGINE_VERSION
from app.search import E5Encoder, Search
from app.components import existing_components

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
