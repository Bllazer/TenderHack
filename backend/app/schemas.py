from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    parent_id: str | None
    ktru_code: str
    name: str
    is_leaf: bool


class AttributeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    value_type: str
    unit: str | None
    aliases: list[str]


class CategoryAttributeOut(AttributeOut):
    required: bool
    allowed_values: list[str]


class InputAttribute(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=512)
    unit: str | None = Field(default=None, max_length=64)


class STECreate(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=1024)
    description: str | None = None
    category_id: str | None = None
    raw_payload: dict = Field(default_factory=dict)


class STEOut(STECreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime


class AnalysisCreate(BaseModel):
    ste_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=1024)
    description: str | None = None
    attributes: list[InputAttribute] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_source(self):
        if not self.ste_id and not self.title:
            raise ValueError("Укажите ste_id или title")
        return self


class NormalizedAttribute(BaseModel):
    attribute_id: str
    name: str
    value: str
    unit: str | None = None
    source: Literal["input", "description"] = "input"


class Issue(BaseModel):
    field: str
    code: str
    message: str


class Candidate(BaseModel):
    category_id: str
    ktru_code: str
    name: str
    matched_attributes: list[str]
    missing_required: list[str]


class AnalysisResult(BaseModel):
    normalized_attributes: list[NormalizedAttribute] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    selected_ktru_code: str | None = None


class AnalysisOut(BaseModel):
    id: str
    ste_id: str | None
    title: str
    description: str | None
    input_attributes: list[InputAttribute]
    status: Literal["needs_data", "needs_input", "ambiguous", "resolved"]
    result: AnalysisResult
    engine_version: str
    created_at: datetime


class FeedbackCreate(BaseModel):
    verdict: Literal["accepted", "rejected", "corrected"]
    corrected_ktru_code: str | None = Field(default=None, max_length=64)
    corrected_attributes: list[InputAttribute] = Field(default_factory=list)
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackOut(FeedbackCreate):
    id: str
    analysis_id: str
    created_at: datetime
