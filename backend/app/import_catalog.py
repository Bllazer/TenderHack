"""Import a normalized organizer dataset; never scrapes external systems."""

import argparse
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.analysis import canon
from app.db import SessionLocal
from app.models import Attribute, Category, CategoryAttribute, STE


class AttributeRow(BaseModel):
    name: str
    value_type: Literal["text", "number", "boolean"] = "text"
    unit: str | None = None
    aliases: list[str] = Field(default_factory=list)


class RuleRow(BaseModel):
    attribute: str
    required: bool = False
    allowed_values: list[str] = Field(default_factory=list)


class CategoryRow(BaseModel):
    ktru_code: str
    name: str
    parent_code: str | None = None
    is_leaf: bool = True
    attributes: list[RuleRow] = Field(default_factory=list)


class STERow(BaseModel):
    source: str
    external_id: str
    title: str
    description: str | None = None
    ktru_code: str | None = None
    raw_payload: dict = Field(default_factory=dict)


class Dataset(BaseModel):
    source_version: str
    attributes: list[AttributeRow] = Field(default_factory=list)
    categories: list[CategoryRow] = Field(default_factory=list)
    ste: list[STERow] = Field(default_factory=list)


def load(path: Path) -> None:
    data = Dataset.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if len({a.name for a in data.attributes}) != len(data.attributes):
        raise ValueError("Duplicate attribute names")
    if len({c.ktru_code for c in data.categories}) != len(data.categories):
        raise ValueError("Duplicate KTRU codes")
    with SessionLocal.begin() as session:
        attrs = {a.name: a for a in session.scalars(select(Attribute)).all()}
        for row in data.attributes:
            obj = attrs.get(row.name)
            if obj is None:
                obj = Attribute(name=row.name)
                session.add(obj)
                attrs[row.name] = obj
            obj.value_type, obj.unit, obj.aliases = row.value_type, row.unit, row.aliases
        session.flush()

        aliases = {}
        for attribute in attrs.values():
            for name in [attribute.name, *attribute.aliases]:
                key = canon(name)
                if not key or (key in aliases and aliases[key] != attribute.name):
                    raise ValueError(f"Empty or conflicting attribute alias: {name!r}")
                aliases[key] = attribute.name

        categories = {c.ktru_code: c for c in session.scalars(select(Category)).all()}
        for row in data.categories:
            obj = categories.get(row.ktru_code)
            if obj is None:
                obj = Category(ktru_code=row.ktru_code)
                session.add(obj)
                categories[row.ktru_code] = obj
            obj.name, obj.is_leaf, obj.source_version = row.name, row.is_leaf, data.source_version
        session.flush()
        for row in data.categories:
            obj = categories[row.ktru_code]
            if row.parent_code and row.parent_code not in categories:
                raise ValueError(f"Unknown parent: {row.parent_code}")
            obj.parent_id = categories[row.parent_code].id if row.parent_code else None
            # An omitted field is a metadata-only update; [] explicitly clears rules.
            if "attributes" not in row.model_fields_set:
                continue
            if len({rule.attribute for rule in row.attributes}) != len(row.attributes):
                raise ValueError(f"Duplicate category rules: {row.ktru_code}")
            # Each supplied category carries its complete current rule set.
            current = session.scalars(select(CategoryAttribute).where(
                CategoryAttribute.category_id == obj.id)).all()
            supplied = {attrs[rule.attribute].id for rule in row.attributes if rule.attribute in attrs}
            for link in current:
                if link.attribute_id not in supplied:
                    session.delete(link)
            for rule in row.attributes:
                if rule.attribute not in attrs:
                    raise ValueError(f"Unknown attribute: {rule.attribute}")
                if attrs[rule.attribute].value_type == "number" and any(
                    not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value.strip()) for value in rule.allowed_values
                ):
                    raise ValueError(f"Invalid numeric allowed value: {rule.attribute}")
                link = session.scalar(select(CategoryAttribute).where(
                    CategoryAttribute.category_id == obj.id,
                    CategoryAttribute.attribute_id == attrs[rule.attribute].id))
                if link is None:
                    link = CategoryAttribute(category_id=obj.id, attribute_id=attrs[rule.attribute].id)
                    session.add(link)
                link.required, link.allowed_values = rule.required, rule.allowed_values
        session.flush()
        by_id = {category.id: category for category in categories.values()}
        for category in categories.values():
            seen = set()
            node = category
            while node.parent_id:
                if node.id in seen:
                    raise ValueError(f"Category hierarchy cycle: {category.ktru_code}")
                seen.add(node.id)
                node = by_id[node.parent_id]
                if node.is_leaf:
                    raise ValueError(f"Leaf category has children: {node.ktru_code}")
        for row in data.ste:
            if row.ktru_code and row.ktru_code not in categories:
                raise ValueError(f"Unknown KTRU: {row.ktru_code}")
            obj = session.scalar(select(STE).where(STE.source == row.source, STE.external_id == row.external_id))
            if obj is None:
                obj = STE(source=row.source, external_id=row.external_id)
                session.add(obj)
            obj.title, obj.description, obj.raw_payload = row.title, row.description, row.raw_payload
            obj.category_id = categories[row.ktru_code].id if row.ktru_code else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import normalized JSON catalog")
    parser.add_argument("path", type=Path)
    load(parser.parse_args().path)
