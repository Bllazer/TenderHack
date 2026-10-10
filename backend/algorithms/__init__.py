"""Алгоритмы C и D поверх очищенного справочника КТРУ.

    from algorithms import AlgorithmC, AlgorithmD, KtruIndex, Units, Fact, Unit

    index = KtruIndex.load()
    units = Units.load()
    pool = index.family_pool("28.24.11.000-00000003")
    facts = [Fact(kind="text", value="От сети", name="Тип питания")]
    result = AlgorithmC.run_c(pool, facts, index, units, type_verified=True)
    question = AlgorithmD.next_question(result.survivor_positions(index),
                                        AlgorithmD.answered_from_facts(facts), index,
                                        type_verified=True, blocking_issues=result.flags.blocking)
"""
from . import AlgorithmC, AlgorithmD
from .AlgorithmC import Units, UnitError, run_c
from .AlgorithmD import answered_from_facts, next_question
from .contract import (
    CResult, Candidate, Exclusion, Fact, FlagSet, Pool, Question, Unresolved, Unit, fact_from_string,
)
from .ktru_index import Alternative, Characteristic, KtruIndex, Position, normalize_name, normalize_text

__all__ = [
    "AlgorithmC", "AlgorithmD",
    "KtruIndex", "Position", "Characteristic", "Alternative", "normalize_name", "normalize_text",
    "Units", "UnitError", "run_c", "next_question", "answered_from_facts",
    "Fact", "Unit", "Pool", "CResult", "Candidate", "Exclusion", "Unresolved", "FlagSet",
    "Question", "fact_from_string",
]
