from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.catalog import Position

ENGINE_VERSION = "stateless-pipeline-v5.1-context-boundaries"

SelectedValues = Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]], Field(min_length=1, max_length=50)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputFact(InputModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(max_length=256)
    value: str = Field(default="", max_length=512)
    values: SelectedValues | None = None
    unit: str | None = Field(default=None, max_length=64)
    unknown: bool = False
    subject: Literal["product", "packaging", "unspecified"] = "product"
    replaces: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def exclusive_value(self):
        if self.values is not None and self.value:
            raise ValueError("Передайте либо value, либо values")
        return self


class Answer(InputModel):
    question_id: str = Field(min_length=1, max_length=512)
    value: str | None = Field(default=None, max_length=512)
    values: SelectedValues | None = None
    unit: str | None = Field(default=None, max_length=64)
    unknown: bool = False

    @model_validator(mode="after")
    def exclusive_value(self):
        if self.values is not None and self.value:
            raise ValueError("Передайте либо value, либо values")
        return self


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


class FactAssignment(BaseModel):
    name: str
    value: str
    unit: str | None = None
    subject: str = "product"
    evidence: str


class Fact(BaseModel):
    id: str
    name: str
    value: str
    values: list[str] | None = None
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
    surface_value: str | None = None
    extraction_evidence: list[str] = Field(default_factory=list)
    card_mappings: list[str] = Field(default_factory=list)
    assignment_options: list[FactAssignment] = Field(default_factory=list)
    uncertainty_reason: str | None = None
    negation_interpretation: Literal["canonical_absence"] | None = None


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


class ElementCheck(BaseModel):
    value: str
    outcome: Literal["compatible", "incompatible", "unknown"]
    reason: str
    value_codes: list[str] = Field(default_factory=list)


class Check(BaseModel):
    property_id: str
    name: str
    outcome: Literal["compatible", "incompatible", "unknown"]
    reason: str
    fact_ids: list[str] = Field(default_factory=list)
    value_codes: list[str] = Field(default_factory=list)
    elements: list[ElementCheck] = Field(default_factory=list)


class BlockingReason(BaseModel):
    code: str
    explanation: str
    property_id: str | None = None
    property_name: str | None = None
    kind: str | None = None
    units: list[str | None] = Field(default_factory=list)
    unsupported_reasons: list[str] = Field(default_factory=list)
    source_issues: list[str] = Field(default_factory=list)
    resolve_blocked_reason: list[str] = Field(default_factory=list)
    rule_source: str | None = None
    applicability: Literal["source", "required", "provided", "potential"] = "source"
    blocks_resolution: bool = True


class CatalogPositionCard(BaseModel):
    catalog_version: str
    position: Position
    blocking_reasons: list[BlockingReason]


class CatalogPropertyField(BaseModel):
    """Одна характеристика области: справочные правила (значения, единицы, обязательность).

    Идентичность характеристики — это `(subject, property_id)`, а НЕ название: в КТРУ
    встречаются разные характеристики с одинаковым названием, и объединять их правила в один
    «достоверный» набор нельзя. Если название делится между несколькими характеристиками,
    у каждой свой `property_id`, а `name_shared` = true — клиент показывает это как разные
    поля, а не как одно.
    """
    property_id: str
    name: str
    subject: str
    aliases: list[str] = Field(default_factory=list)
    required: bool = False
    multiple: bool = False
    options: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    question_id: str | None = None
    source: str | None = None
    name_shared: bool = False


class CategoryPropertiesOut(BaseModel):
    """Справочные характеристики категории (не путать с фактами пользователя)."""
    category_id: str
    catalog_version: str
    complete: bool
    properties: list[CatalogPropertyField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    ktru_code: str
    name: str
    okpd2: str | None
    outcome: Literal["compatible", "incompatible", "unknown"]
    checks: list[Check]
    missing_required: list[str]
    has_characteristics: bool
    blocking_reasons: list[BlockingReason] = Field(default_factory=list)
    exclusion_reasons: list[Issue] = Field(default_factory=list)


class QuestionOption(BaseModel):
    value: str
    label: str


def _number(value: float) -> str:
    return f"{value:g}"


class QuestionRange(BaseModel):
    """Допустимый интервал значения — берётся из найденных позиций, а не из констант."""
    lower: float | None = None
    upper: float | None = None
    unit: str | None = None

    def text(self) -> str:
        if self.lower is not None and self.upper is not None:
            body = f"от {_number(self.lower)} до {_number(self.upper)}"
        elif self.lower is not None:
            body = f"не меньше {_number(self.lower)}"
        elif self.upper is not None:
            body = f"не больше {_number(self.upper)}"
        else:
            return ""
        return f"{body} {self.unit or ''}".strip()


class Question(BaseModel):
    id: str
    kind: Literal["conflict", "category", "property", "code", "correction"]
    title: str
    reason: str
    property_name: str | None = None
    options: list[QuestionOption] = Field(default_factory=list)
    unit: str | None = None
    allow_unknown: bool = True
    multiple: bool = False
    #: Допустимые границы числового значения у оставшихся позиций (объединение интервалов
    #: их характеристик) и готовая подсказка для интерфейса. Пусто, если характеристика не
    #: числовая: тогда ограничивать нечем, и вопрос остаётся свободным.
    ranges: list[QuestionRange] = Field(default_factory=list)
    hint: str | None = None


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
    card_schema_version: str | None = None
    card_schema_evidence: list[str] = Field(default_factory=list)


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


class BatchCreate(InputModel):
    """Разбор загруженного CSV: одна строка файла — одна строка ввода товара."""

    csv: str | None = Field(default=None, max_length=2_000_000)
    csv_base64: str | None = Field(default=None, max_length=4_000_000)
    filename: str | None = Field(default=None, max_length=256)
    candidate_limit: int = Field(default=20, ge=1, le=100)
    run_limit: int = Field(default=50, ge=1, le=100)
    skip_header: bool = True
    #: Явный выбор разделителя, когда формат файла неоднозначен. Разбор никогда не режет
    #: строку по случайной запятой: десятичная запятая в числе разделителем не считается.
    csv_delimiter: str | None = Field(default=None, pattern=r"^(;|,|\t|\|)$")

    @model_validator(mode="after")
    def one_source(self):
        if (self.csv is None) == (self.csv_base64 is None):
            raise ValueError("Передайте либо csv, либо csv_base64")
        return self


class BatchRow(BaseModel):
    """Компактный результат по одной строке файла."""

    line: int
    description: str
    status: Literal["needs_data", "needs_input", "ambiguous", "resolved", "error"]
    selected_ktru_code: str | None = None
    candidate_count: int = 0
    excluded_count: int = 0
    outcome: str | None = None
    top_candidates: list[str] = Field(default_factory=list)
    category_id: str | None = None
    scope_complete: bool = False
    question_kind: str | None = None
    question_title: str | None = None
    question_property: str | None = None
    issue_codes: list[str] = Field(default_factory=list)
    reason: str | None = None
    verification: Verification = Field(default_factory=Verification)
    error: str | None = None


class BatchOut(BaseModel):
    engine_version: str = ENGINE_VERSION
    catalog_version: str
    filename: str | None = None
    encoding: str
    delimiter: str
    header_skipped: bool = False
    rows_total: int = 0
    rows_skipped: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    truncated: bool = False
    counts: dict = Field(default_factory=dict)
    rows: list[BatchRow] = Field(default_factory=list)


class CsvRowsOut(BaseModel):
    """Только разбор файла: строки для поштучного прогона через /api/v1/analyses."""

    engine_version: str = ENGINE_VERSION
    catalog_version: str
    filename: str | None = None
    encoding: str
    delimiter: str
    header_skipped: bool = False
    rows_total: int = 0
    rows: list[dict] = Field(default_factory=list)
    rows_skipped: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    truncated: bool = False
