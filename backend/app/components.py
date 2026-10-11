"""Integration ports; default adapters call existing algorithms without reimplementing them."""
from dataclasses import dataclass, field
from typing import Callable, Protocol

from app.catalog import Position
from app.schemas import Candidate, Correction, Fact, Fragment, Issue, CategoryCandidate


@dataclass
class ExtractionResult:
    facts: list[Fact]
    unparsed: list[Fragment]
    corrections: list[Correction] = field(default_factory=list)


class Extractor(Protocol):
    def extract(self, text: str, names: tuple[str, ...], *, scoped_properties=()) -> ExtractionResult: ...


@dataclass(frozen=True)
class SearchInput:
    description: str
    facts: tuple[Fact, ...]


class Searcher(Protocol):
    def find_state(self, state: SearchInput) -> list[CategoryCandidate]: ...


class ExistingSearcher:
    def __init__(self, search):
        self.search = search

    def find_state(self, state):
        """Поисковый запрос из описания И актуальных фактов пользователя.

        Факты влияют на представление запроса: уточнение характеристики должно менять
        подсказки типов, а не только проверку уже найденной категории. В запрос идут только
        положительные именованные значения: имя характеристики и её значение. Технические
        идентификаторы, ответы accept/reject, флаги проверки, отрицания и факты без имени
        (в том числе «не знаю») в запрос не попадают — иначе поиск искал бы по служебным
        строкам. Каталог при этом не пересчитывается: меняется только текст запроса.
        """
        return self.search.find(search_text(state.description, state.facts))


#: Служебные значения ответов: это выбор пользователя в интерфейсе, а не свойство товара.
_SERVICE_VALUES = ("accept:", "reject", "не знаю", "не задано")


def search_text(description: str, facts) -> str:
    """Текст запроса: исходное описание плюс именованные положительные факты."""
    parts = [description.strip()]
    seen = {part.casefold() for part in parts}
    for fact in facts or ():
        name = (getattr(fact, "name", "") or "").strip()
        value = (getattr(fact, "value", "") or "").strip()
        if not name or not value or getattr(fact, "unknown", False) or getattr(fact, "negated", False):
            continue
        if any(value.casefold().startswith(marker) for marker in _SERVICE_VALUES):
            continue
        if getattr(fact, "source", "") == "answer" and getattr(fact, "values", None):
            value = " ".join(fact.values)
        part = f"{name} {value}"
        if part.casefold() in seen:
            continue
        seen.add(part.casefold())
        parts.append(part)
    return "; ".join(part for part in parts if part)


class Checker(Protocol):
    def evaluate(self, position: Position, facts: list[Fact], unit_resolver: Callable) -> tuple[Candidate, list[Issue], list[Correction]]: ...


@dataclass(frozen=True)
class Components:
    extractor: Extractor | None
    checker: Checker | None
    questions: Callable | None

    @property
    def missing(self):
        return tuple(name for name, component in (("extractor", self.extractor), ("checker", self.checker),
                                                  ("questions", self.questions)) if component is None)


class ExistingExtractor:
    def __init__(self, cards=None):
        self.cards = cards

    def context(self, category_id, properties):
        return self.cards.context(category_id, properties) if self.cards else (properties, (), ())

    def normalize_context(self, facts, category_id, properties):
        from app.extraction import canonicalize_facts
        canonicalize_facts(facts, properties, self.cards.lexicon if self.cards and category_id else {})

    def extract_context(self, text, names, *, scoped_properties=(), category_id=None):
        from app.extraction import extract
        lexicon = self.cards.lexicon if self.cards and category_id else {}
        # Провенанс автоматических исправлений имён: замена не спрашивает разрешения,
        # но и не молчит — в ответе видно ««Мошность» → «Мощность»».
        corrections: list = []
        facts, unparsed = extract(text, names, scoped_properties=scoped_properties, lexicon=lexicon,
                                  corrections=corrections)
        return ExtractionResult(facts, unparsed, corrections)

    def extract(self, text, names, *, scoped_properties=()):
        from app.extraction import extract
        corrections: list = []
        facts, unparsed = extract(text, names, scoped_properties=scoped_properties,
                                  corrections=corrections)
        return ExtractionResult(facts, unparsed, corrections)


class ExistingChecker:
    def evaluate(self, position, facts, unit_resolver):
        from app.checks import evaluate
        return evaluate(position, facts, unit_resolver)


def existing_components(data=None):
    from app.questions import next_question
    from app.card_schemas import CardSchemas
    from app.config import settings
    cards = CardSchemas(settings.card_schema_file)
    extractor = ExistingExtractor(cards)
    if data is not None and data.categories:
        try:
            from app.algorithm_adapter import AlgorithmChecker, AlgorithmQuestions, make_index, make_units
        except ModuleNotFoundError as exc:
            if not (exc.name or "").startswith("algorithms"):
                raise
            return Components(extractor, None, None)
        index = make_index(data)
        return Components(extractor, AlgorithmChecker(index, make_units(data)), AlgorithmQuestions(index))
    return Components(extractor, ExistingChecker(), next_question)
