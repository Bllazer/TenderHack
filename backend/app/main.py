from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.analysis import analyze
from app.config import settings
from app.db import SessionDep
from app.models import Analysis, Attribute, Category, CategoryAttribute, Feedback, STE
from app.schemas import (AnalysisCreate, AnalysisOut, AnalysisResult, CategoryAttributeOut, CategoryOut,
                         FeedbackCreate, FeedbackOut, InputAttribute, STECreate, STEOut)

app = FastAPI(title="TenderHack STE API", version="0.1.0",
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=[s.strip() for s in settings.cors_origins.split(",") if s.strip()],
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/api/health", tags=["health"])
@app.get("/api/health/live", tags=["health"])
def live() -> dict:
    return {"status": "ok"}


@app.get("/api/health/ready", tags=["health"])
def ready(session: SessionDep) -> dict:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database_unavailable") from exc
    return {"status": "ok"}


@app.get("/api/v1/catalog/status", tags=["catalog"])
def catalog_status(session: SessionDep) -> dict:
    return {"categories": session.scalar(select(func.count()).select_from(Category)) or 0,
            "attributes": session.scalar(select(func.count()).select_from(Attribute)) or 0,
            "ste": session.scalar(select(func.count()).select_from(STE)) or 0}


@app.get("/api/v1/catalog/categories", response_model=list[CategoryOut], tags=["catalog"])
def categories(session: SessionDep, parent_id: str | None = None, q: str | None = None,
               limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    stmt = select(Category)
    if parent_id:
        stmt = stmt.where(Category.parent_id == parent_id)
    if q:
        stmt = stmt.where(Category.name.ilike(f"%{q}%"))
    return session.scalars(stmt.order_by(Category.ktru_code).offset(offset).limit(limit)).all()


@app.get("/api/v1/catalog/categories/{category_id}/attributes", response_model=list[CategoryAttributeOut], tags=["catalog"])
def category_attributes(category_id: str, session: SessionDep):
    if session.get(Category, category_id) is None:
        raise HTTPException(404, "category_not_found")
    rows = session.execute(select(Attribute, CategoryAttribute).join(CategoryAttribute)
                           .where(CategoryAttribute.category_id == category_id).order_by(Attribute.name)).all()
    return [CategoryAttributeOut(id=attribute.id, name=attribute.name, value_type=attribute.value_type,
                                 unit=attribute.unit, aliases=attribute.aliases, required=rule.required,
                                 allowed_values=rule.allowed_values) for attribute, rule in rows]


@app.get("/api/v1/ste", response_model=list[STEOut], tags=["ste"])
def list_ste(session: SessionDep, q: str | None = None, limit: int = Query(50, ge=1, le=100),
             offset: int = Query(0, ge=0)):
    stmt = select(STE)
    if q:
        stmt = stmt.where(STE.title.ilike(f"%{q}%"))
    return session.scalars(stmt.order_by(STE.created_at.desc(), STE.id).offset(offset).limit(limit)).all()


@app.post("/api/v1/ste", response_model=STEOut, status_code=201, tags=["ste"])
def create_ste(body: STECreate, session: SessionDep):
    if body.category_id and session.get(Category, body.category_id) is None:
        raise HTTPException(422, "unknown_category_id")
    item = STE(**body.model_dump())
    session.add(item)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "source_external_id_exists") from exc
    session.refresh(item)
    return item


@app.get("/api/v1/ste/{ste_id}", response_model=STEOut, tags=["ste"])
def get_ste(ste_id: str, session: SessionDep):
    item = session.get(STE, ste_id)
    if item is None:
        raise HTTPException(404, "ste_not_found")
    return item


def to_analysis_out(item: Analysis) -> AnalysisOut:
    return AnalysisOut(id=item.id, ste_id=item.ste_id, title=item.title, description=item.description,
                       input_attributes=[InputAttribute.model_validate(x) for x in item.input_attributes],
                       status=item.status, result=AnalysisResult.model_validate(item.result),
                       engine_version=item.engine_version, created_at=item.created_at)


@app.post("/api/v1/analyses", response_model=AnalysisOut, status_code=201, tags=["analysis"])
def create_analysis(body: AnalysisCreate, session: SessionDep):
    ste = session.get(STE, body.ste_id) if body.ste_id else None
    if body.ste_id and ste is None:
        raise HTTPException(404, "ste_not_found")
    title = body.title or ste.title
    description = body.description if body.description is not None else (ste.description if ste else None)
    status_name, result = analyze(session, body.attributes)
    item = Analysis(ste_id=body.ste_id, title=title, description=description,
                    input_attributes=[x.model_dump() for x in body.attributes],
                    status=status_name, result=result.model_dump())
    session.add(item)
    session.commit()
    session.refresh(item)
    return to_analysis_out(item)


@app.get("/api/v1/analyses/{analysis_id}", response_model=AnalysisOut, tags=["analysis"])
def get_analysis(analysis_id: str, session: SessionDep):
    item = session.get(Analysis, analysis_id)
    if item is None:
        raise HTTPException(404, "analysis_not_found")
    return to_analysis_out(item)


@app.post("/api/v1/analyses/{analysis_id}/feedback", response_model=FeedbackOut, status_code=201, tags=["analysis"])
def create_feedback(analysis_id: str, body: FeedbackCreate, session: SessionDep):
    if session.get(Analysis, analysis_id) is None:
        raise HTTPException(404, "analysis_not_found")
    if body.verdict == "corrected" and not (body.corrected_ktru_code or body.corrected_attributes):
        raise HTTPException(422, "correction_required")
    item = Feedback(analysis_id=analysis_id, verdict=body.verdict,
                    corrected_ktru_code=body.corrected_ktru_code,
                    corrected_attributes=[x.model_dump() for x in body.corrected_attributes], comment=body.comment)
    session.add(item)
    session.commit()
    session.refresh(item)
    return FeedbackOut(id=item.id, analysis_id=item.analysis_id, verdict=item.verdict,
                       corrected_ktru_code=item.corrected_ktru_code,
                       corrected_attributes=[InputAttribute.model_validate(x) for x in item.corrected_attributes],
                       comment=item.comment, created_at=item.created_at)
