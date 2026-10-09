"""Validated immutable catalog snapshot, never a request store."""
import hashlib
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AllowedValue(Frozen):
    kind: Literal["enum", "number", "interval"]
    value: str | None = None
    lower: str | None = None
    upper: str | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    unit: str | None = None
    raw_unit: str | None = None
    okei_code: str | None = None
    value_code: str | None = None
    source: str = Field(min_length=1)


class Property(Frozen):
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    source: Literal["ktru", "portal", "spgz"] = "ktru"
    evidence: str = ""
    subject: Literal["product", "packaging"] = "product"
    value_type: Literal["text", "number", "boolean", "unsupported"]
    required: bool | None = None
    verified: bool = False
    allowed: tuple[AllowedValue, ...] = ()
    semantic_key: str | None = None


class Position(Frozen):
    code: str
    name: str
    parent_code: str | None = None
    is_template: bool | None = None
    status: Literal["actual", "terminated", "unknown"] = "unknown"
    okpd2: str | None = None
    no_new_features: bool | None = None
    no_new_features_evidence: str | None = None
    source_path: str
    raw_json: str = "{}"
    properties: tuple[Property, ...] = ()
    quarantined: bool = False


class Category(Frozen):
    id: str
    name: str
    search_text: str
    position_codes: tuple[str, ...]
    link_verified: bool = False
    coverage_verified: bool = False
    evidence: str = ""


class Catalog(Frozen):
    format_version: Literal[1] = 1
    catalog_version: str
    source_description: str
    validation_report: str
    synthetic: bool = False
    categories: tuple[Category, ...] = ()
    positions: tuple[Position, ...] = ()

    @model_validator(mode="after")
    def validate_links(self):
        codes = {p.code: p for p in self.positions}
        if len(codes) != len(self.positions) or len({c.id for c in self.categories}) != len(self.categories):
            raise ValueError("Duplicate catalog identifiers")
        for position in self.positions:
            if position.quarantined:
                raise ValueError("Quarantined positions must not enter the working catalog")
            if len({p.id for p in position.properties}) != len(position.properties):
                raise ValueError("Duplicate contextual property identifiers")
            seen = {position.code}
            parent = position.parent_code
            while parent:
                if parent not in codes or parent in seen:
                    raise ValueError("Unknown parent or hierarchy cycle")
                seen.add(parent)
                if codes[parent].is_template is not True:
                    raise ValueError("Parent must explicitly be a template")
                parent = codes[parent].parent_code
            for prop in position.properties:
                if prop.verified and not prop.evidence:
                    raise ValueError("Verified properties need source evidence")
                for allowed in prop.allowed:
                    validate_allowed(allowed)
        for category in self.categories:
            if any(code not in codes for code in category.position_codes):
                raise ValueError("Unknown category-position link")
            if (category.link_verified or category.coverage_verified) and not category.evidence:
                raise ValueError("Verified scope needs source evidence")
        return self


def validate_allowed(value: AllowedValue) -> None:
    if value.kind == "enum":
        if not value.value:
            raise ValueError("Empty allowed enum")
        return
    numbers = [value.value] if value.kind == "number" else [value.lower, value.upper]
    if not any(item is not None for item in numbers):
        raise ValueError("Empty numeric constraint")
    for item in numbers:
        if item is not None:
            try:
                finite = Decimal(item).is_finite()
            except InvalidOperation as exc:
                raise ValueError("Invalid numeric constraint") from exc
            if not finite:
                raise ValueError("Non-finite numeric constraint")
    if value.lower is not None and value.upper is not None:
        if Decimal(value.lower) > Decimal(value.upper):
            raise ValueError("Reversed interval")


@dataclass(frozen=True)
class Snapshot:
    catalog: Catalog
    positions: Mapping[str, Position]
    categories: Mapping[str, Category]
    vectors: np.ndarray | None = None
    tokens: tuple[tuple[str, ...], ...] = ()
    model_revision: str | None = None
    problem: str | None = None
    scopes: Mapping = field(default_factory=lambda: MappingProxyType({}))
    extraction_names: tuple[str, ...] = ()
    answer_properties: Mapping[str, Property] = field(default_factory=lambda: MappingProxyType({}))

    def pool(self, category_id: str) -> tuple[tuple[Position, ...], bool]:
        if category_id in self.scopes:
            return self.scopes[category_id]
        category = self.categories[category_id]
        visited: set[str] = set()
        found: list[Position] = []
        complete = category.link_verified and category.coverage_verified
        children: dict[str, list[Position]] = {}
        for position in self.positions.values():
            if position.parent_code:
                children.setdefault(position.parent_code, []).append(position)

        def expand(code: str) -> None:
            nonlocal complete
            if code in visited:
                return
            visited.add(code)
            position = self.positions[code]
            if position.status == "terminated":
                return
            if position.status != "actual" or position.is_template is None:
                complete = False
                return
            if position.is_template:
                descendants = children.get(code, [])
                if not descendants:
                    complete = False
                for child in descendants:
                    expand(child.code)
            else:
                found.append(position)
        for code in category.position_codes:
            expand(code)
        return tuple(sorted(found, key=lambda p: p.code)), complete and bool(found)


def snapshot(catalog: Catalog, **kwargs) -> Snapshot:
    from app.questions import property_question_id
    names = tuple(sorted({name for position in catalog.positions for prop in position.properties if prop.verified
                          for name in (prop.name, *prop.aliases)}))
    data = Snapshot(catalog, MappingProxyType({p.code: p for p in catalog.positions}),
                    MappingProxyType({c.id: c for c in catalog.categories}), extraction_names=names, **kwargs)
    scopes = {category.id: data.pool(category.id) for category in catalog.categories}
    answer_properties = {property_question_id(category_id, prop): prop for category_id, (positions, _) in scopes.items()
                         for position in positions for prop in position.properties}
    return replace(data, scopes=MappingProxyType(scopes), answer_properties=MappingProxyType(answer_properties))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_snapshot(directory: Path, allow_synthetic: bool = False) -> Snapshot:
    from app.search import ANALYZER_VERSION
    catalog = Catalog.model_validate_json((directory / "catalog.json").read_text(encoding="utf-8"))
    if catalog.synthetic and not allow_synthetic:
        raise ValueError("Synthetic catalogs are test-only")
    if not catalog.categories or not catalog.positions:
        return snapshot(catalog, problem="catalog_empty")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["catalog_version"] != catalog.catalog_version or manifest["analyzer_version"] != ANALYZER_VERSION:
        raise ValueError("Catalog/index version mismatch")
    if manifest["model_id"] != "intfloat/multilingual-e5-small" or len(manifest["model_revision"]) != 40:
        raise ValueError("Unpinned or unexpected embedding model")
    for name in ("catalog.json", "vectors.npy", "tokens.json"):
        if manifest["sha256"][name] != digest(directory / name):
            raise ValueError("Catalog/index checksum mismatch")
    vectors = np.load(directory / "vectors.npy", allow_pickle=False)
    if vectors.shape != (len(catalog.categories), 384) or not np.isfinite(vectors).all():
        raise ValueError("Invalid embedding matrix")
    if not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3):
        raise ValueError("Embeddings must be normalized")
    vectors.setflags(write=False)
    tokens = tuple(tuple(row) for row in json.loads((directory / "tokens.json").read_text(encoding="utf-8")))
    if len(tokens) != len(catalog.categories):
        raise ValueError("Invalid lexical index alignment")
    return snapshot(catalog, vectors=vectors, tokens=tokens, model_revision=manifest["model_revision"])
