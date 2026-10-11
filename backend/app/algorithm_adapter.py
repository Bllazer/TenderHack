"""C/D bridge over the existing immutable snapshot; strict backend checks remain a gate."""
import json
from types import MappingProxyType

from algorithms.AlgorithmC import Units, _norm_unit, run_c
from algorithms.AlgorithmD import next_question as algorithm_question
from algorithms.contract import Fact as AlgorithmFact
from algorithms.ktru_index import Alternative, Characteristic, KtruIndex, Position as AlgorithmPosition
from app.checks import evaluate
from app.questions import next_question, property_question, property_question_id
from app.schemas import Issue
from app.structured import to_structured


def make_index(data):
    positions, families, roots = {}, {}, []
    for position in data.positions.values():
        chars = []
        for prop in position.properties:
            alternatives = [Alternative(kind={"enum":"text", "number":"number", "interval":"range"}[item.kind],
                text=item.value if item.kind == "enum" else None,
                number=float(item.value) if item.kind == "number" else None,
                lo=float(item.lower) if item.lower is not None else None,
                hi=float(item.upper) if item.upper is not None else None,
                lo_strict=not item.lower_inclusive, hi_strict=not item.upper_inclusive,
                unit_code=item.okei_code, unit_text=item.unit, value_code=item.value_code) for item in prop.allowed]
            chars.append(Characteristic(prop.id, prop.name, prop.source_type or ("2" if prop.value_type == "number" else "1"),
                                        prop.kind, prop.required is True, prop.choice_type, alternatives,
                                        prop.numeric_semantics, prop.numeric_semantics_evidence))
        current = AlgorithmPosition(position.code, position.name, position.is_template is True,
            "ACTIVE" if position.status == "actual" else position.status, bool(position.properties),
            bool(position.source_issues or position.resolve_blocked is True), list(position.resolve_blocked_reason),
            position.parent_code, None, position.no_new_features, None, chars)
        positions[position.code] = current
        if current.parent_code:
            families.setdefault(current.parent_code, []).append(current)
        else:
            roots.append(current)
    return KtruIndex(MappingProxyType(positions), MappingProxyType({k:tuple(v) for k,v in families.items()}),
                     tuple(roots), data.catalog.catalog_version)


def make_units(data):
    units = Units()
    units.by_code = {unit.code: unit.model_dump() for unit in data.catalog.units}
    units.aliases = {_norm_unit(alias.raw): alias.model_dump() for alias in data.catalog.unit_aliases}
    _add_word_units(units, data)
    return units


def _add_word_units(units, data) -> None:
    """Дополняет алиасы поставки полными словами единиц («метров», «килограммов»).

    В поставке лежат сокращения и коды ОКЕИ, поэтому «молоток 200 метров» не разбирался
    как измерение и вброс проходил без проверки. Слова берутся из `app.units` и
    привязываются к тем же кодам ОКЕИ, что и сокращения: справочник поставки остаётся
    источником кода, а слово — только написание. Существующие алиасы не переопределяются.
    """
    from app.units import UNITS

    template: dict[str, str] = {}
    for unit in data.catalog.units:
        for spelling in unit.spellings:
            known = UNITS.get(spelling.name.casefold().replace("ё", "е"))
            if known is not None and known.name not in template:
                template[known.name] = unit.code
    for alias, known in UNITS.items():
        if alias in units.aliases or len(alias) < 3 or not alias.isalpha():
            continue
        code = template.get(known.name)
        if code is None:
            continue
        sample = next((item for item in units.aliases.values() if item.get("okei_code") == code), None)
        if sample is None:
            continue
        units.aliases[alias] = {**sample, "raw": alias,
                                "raw_json": json.dumps({"raw": alias, "okei_code": code,
                                                        "okei_name": sample.get("okei_name"),
                                                        "resolved_by": "app.units", "global_safe": True},
                                                       ensure_ascii=False)}


class AlgorithmChecker:
    def __init__(self, index, units):
        self.index, self.units = index, units

    def evaluate(self, position, facts, unit_resolver):
        candidate, issues, corrections = evaluate(position, facts, unit_resolver)
        structured = to_structured(facts, position)
        # Mapping remains contextual in backend. Unmapped additions are not C evidence.
        evidence = [AlgorithmFact.from_dict(fact.model_dump()) for fact in structured if fact.def_id]
        result = run_c([self.index.by_code[position.code]], evidence, self.index, self.units,
                       type_verified=False, catalog_scope_complete=False)
        verdict = result.candidates[0].verdict if result.candidates else "unknown"
        if candidate.outcome == "compatible" and verdict != "compatible":
            candidate.outcome = "unknown"
            issues.append(Issue(code="algorithm_verification_incomplete", candidate_code=position.code,
                explanation="Проверка C не подтвердила все значения; требуется уточнение"))
        return candidate, issues, corrections


class AlgorithmQuestions:
    def __init__(self, index):
        self.index = index

    def __call__(self, category_id, categories, positions, candidates, facts, issues, answered, ready, confirmed):
        fallback, indistinguishable = next_question(category_id, categories, positions, candidates, facts,
                                                   issues, answered, ready, confirmed)
        if not category_id or indistinguishable or (fallback and fallback.kind != "property"):
            return fallback, indistinguishable
        pool = [self.index.by_code[candidate.ktru_code] for candidate in candidates]
        known = set()
        for position in positions:
            for prop in position.properties:
                if property_question_id(category_id, prop) in answered:
                    known.add(prop.id)
        for candidate in candidates:
            known.update(check.property_id for check in candidate.checks if check.outcome == "compatible")
        proposal = algorithm_question(pool, known, self.index, type_verified=True)
        if proposal.stop_reason or not proposal.name:
            return fallback, indistinguishable
        properties = [prop for position in positions if position.code in {candidate.ktru_code for candidate in candidates}
                      for prop in position.properties if prop.name == proposal.name]
        keys = {(prop.semantic_key or prop.id, prop.subject) for prop in properties}
        if not properties or len(keys) != 1 or any(not prop.verified for prop in properties):
            return fallback, indistinguishable
        variants = {item.model_dump_json(): item for prop in properties for item in prop.allowed}
        prop = properties[0].model_copy(update={"allowed":tuple(variants.values())})
        return property_question(category_id, prop, "Различает позиции" if proposal.reason == "splits_candidates"
                                 else "Нужно для проверки обязательного поля"), False
