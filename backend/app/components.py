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
    def extract(self, text: str, names: tuple[str, ...]) -> ExtractionResult: ...


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
        # Preserve the existing algorithm's text interface; ML can consume both fields.
        return self.search.find(state.description)


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
    def extract(self, text, names):
        from app.extraction import extract
        facts, unparsed = extract(text, names)
        return ExtractionResult(facts, unparsed)


class ExistingChecker:
    def evaluate(self, position, facts, unit_resolver):
        from app.checks import evaluate
        return evaluate(position, facts, unit_resolver)


def existing_components():
    from app.questions import next_question
    return Components(ExistingExtractor(), ExistingChecker(), next_question)
