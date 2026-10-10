from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ENGINE_VERSION = "stateless-pipeline-v2-cd-gated"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputFact(InputModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=256)
    value: str = Field(max_length=512)
    unit: str | None = Field(default=None, max_length=64)
    unknown: bool = False
    subject: Literal["product", "packaging", "unspecified"] = "product"
    replaces: str | None = Field(default=None, max_length=128)


class Answer(InputModel):
    question_id: str = Field(min_length=1, max_length=512)
    value: str | None = Field(default=None, max_length=512)
    unit: str | None = Field(default=None, max_length=64)
    unknown: bool = False


class AnalysisCreate(InputModel):
    description: str = Field(min_length=1, max_length=12000)
    attributes: list[InputFact] = Field(default_factory=list, max_length=100)
    additional: list[Annotated[str, Field(max_length=1024)]] = Field(default_factory=list, max_length=100)
    confirmed_category_id: str | None = Field(default=None, min_length=1, max_length=128)
    answers: list[Answer] = Field(default_factory=list, max_length=100)
    confirmed_ktru_code: str | None = Field(default=None, min_length=1, max_length=64)
    candidate_limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_state(self):
        if not self.description.strip():
            raise ValueError("Введите описание товара")
        if len({fact.id for fact in self.attributes}) != len(self.attributes):
            raise ValueError("Идентификаторы фактов должны быть уникальны")
        if len({answer.question_id for answer in self.answers}) != len(self.answers):
            raise ValueError("Передайте текущее значение каждого ответа один раз")
        return self


class Issue(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"] = "error"
    explanation: str
    fact_id: str | None = None
    property_id: str | None = None
    candidate_code: str | None = None
    rule_source: str | None = None
    suggested_action: str = "Проверьте исходные сведения"
    confirmation_required: bool = False


class Correction(BaseModel):
    fact_id: str
    kind: Literal["auto_normalization", "proposed_text_fix", "proposed_fact_change"]
    original: str
    normalized: str
    explanation: str
    confirmation_required: bool = False


class Fact(BaseModel):
    id: str
    name: str
    value: str
    unit: str | None = None
    raw: str
    span: tuple[int, int] | None = None
    source: Literal["text", "input", "answer"]
    subject: str = "product"
    negated: bool = False
    unknown: bool = False
    normalized_value: str | None = None
    normalized_unit: str | None = None
    mappings: list[str] = Field(default_factory=list)


class Fragment(BaseModel):
    id: str | None = None
    raw: str
    span: tuple[int, int] | None = None


class CategoryCandidate(BaseModel):
    id: str
    name: str
    rank: int
    rank_score: float
    reasons: list[str]


class Check(BaseModel):
    property_id: str
    name: str
    outcome: Literal["compatible", "incompatible", "unknown"]
    reason: str
    fact_ids: list[str] = Field(default_factory=list)
    value_codes: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    ktru_code: str
    name: str
    okpd2: str | None
    outcome: Literal["compatible", "incompatible", "unknown"]
    checks: list[Check]
    missing_required: list[str]
    has_characteristics: bool


class QuestionOption(BaseModel):
    value: str
    label: str


class Question(BaseModel):
    id: str
    kind: Literal["conflict", "category", "property", "code", "correction"]
    title: str
    reason: str
    property_name: str | None = None
    options: list[QuestionOption] = Field(default_factory=list)
    unit: str | None = None
    allow_unknown: bool = True


class Verification(BaseModel):
    type_verified: bool = False
    ktru_unique: bool = False
    fields_complete: bool = False
    value_verified: bool = False
    catalog_scope_complete: bool = False
    value_indistinguishable: bool = False
    confirmed_by_user: bool = False
    blocking_issues: bool = False


class Scope(BaseModel):
    category_id: str | None = None
    position_count: int = 0
    evaluated_count: int = 0
    complete: bool = False


class AnalysisOut(BaseModel):
    status: Literal["needs_data", "needs_input", "ambiguous", "resolved"]
    original_text: str
    normalized_text: str
    original_facts: list[Fact] = Field(default_factory=list)
    facts: list[Fact]
    unparsed: list[Fragment]
    additional: list[Fragment]
    categories: list[CategoryCandidate] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    excluded: list[Candidate] = Field(default_factory=list)
    candidate_count: int = 0
    excluded_count: int = 0
    scope: Scope = Field(default_factory=Scope)
    issues: list[Issue] = Field(default_factory=list)
    corrections: list[Correction] = Field(default_factory=list)
    next_question: Question | None = None
    next_steps: list[str] = Field(default_factory=list)
    verification: Verification = Field(default_factory=Verification)
    selected_ktru_code: str | None = None
    catalog_version: str
    engine_version: str = ENGINE_VERSION


class ErrorDetail(BaseModel):
    code: str
    field: str | None = None
    message: str | None = None
    catalog_version: str | None = None
    missing_resources: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: ErrorDetail | list[dict]


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Ready(Health):
    catalog_version: str
    engine_version: str


class CatalogStatus(BaseModel):
    categories: int
    positions: int
    catalog_version: str
    ready: bool
    reason: str | None
    catalog_loaded: bool = False
    missing_resources: list[str] = Field(default_factory=list)
    templates: int = 0
    final_positions: int = 0
