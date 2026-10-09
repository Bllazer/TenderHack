from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.analysis import analyze
from app.db import Base
from app.models import Attribute, Category, CategoryAttribute
from app.schemas import InputAttribute


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_empty_catalog_is_explicit():
    with session() as db:
        status, result = analyze(db, [])
        assert status == "needs_data"
        assert result.issues[0].code == "catalog_empty"


def test_narrowing_and_unit_error():
    with session() as db:
        material = Attribute(name="Материал", value_type="text", aliases=[], unit=None)
        cup1 = Category(ktru_code="DEMO-1", name="Стакан бумажный", is_leaf=True)
        cup2 = Category(ktru_code="DEMO-2", name="Стакан пластиковый", is_leaf=True)
        db.add_all([material, cup1, cup2])
        db.flush()
        db.add_all([
            CategoryAttribute(category_id=cup1.id, attribute_id=material.id, required=True, allowed_values=["бумага"]),
            CategoryAttribute(category_id=cup2.id, attribute_id=material.id, required=True, allowed_values=["пластик"]),
        ])
        db.flush()
        status, result = analyze(db, [])
        assert status == "ambiguous" and len(result.candidates) == 2
        status, result = analyze(db, [InputAttribute(name="Материал", value="бумага")])
        assert status == "resolved" and result.selected_ktru_code == "DEMO-1"
        status, result = analyze(db, [InputAttribute(name="Материал", value="стекло")])
        assert status == "needs_input" and result.issues[-1].code == "no_match"


def test_invalid_unit_cannot_resolve():
    with session() as db:
        volume = Attribute(name="Объем", value_type="number", unit="мл", aliases=[])
        cup = Category(ktru_code="DEMO-3", name="Стакан", is_leaf=True)
        db.add_all([volume, cup])
        db.flush()
        db.add(CategoryAttribute(category_id=cup.id, attribute_id=volume.id, required=True, allowed_values=[]))
        db.flush()
        status, result = analyze(db, [InputAttribute(name="Объем", value="200", unit="л")])
        assert status == "needs_input"
        assert result.issues[0].code == "unit_mismatch"
