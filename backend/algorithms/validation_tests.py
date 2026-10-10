"""Проверки AlgorithmC и AlgorithmD на фикстуре и на синтетических данных.

Запуск: python -m algorithms.validation_tests
        (корпус КТРУ не нужен — используется samples/ktru_mini_index.json)
"""
from __future__ import annotations

import json
import os
import unittest

from .AlgorithmC import Units, run_c
from .AlgorithmD import answered_from_facts, next_question
from .contract import Fact, fact_from_string
from .ktru_index import Characteristic, KtruIndex, Position, Alternative

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
DRILL_TEMPLATE = "28.24.11.000-00000003"
SAME_STRUCTURE_TEMPLATE = "71.12.35.110-00004"


# ------------------------------------------------------------------ синтетика
def char_text(name, code, values, required=True, kind="2"):
    return Characteristic(code=code, name=name, type="1", kind=kind, is_required=required,
                          choice_type="2", alternatives=[Alternative(kind="text", text=v) for v in values])


def char_range(name, code, ranges, unit=("003", "Миллиметр"), required=True, kind="2"):
    alts = [Alternative(kind="range", lo=lo, lo_strict=ls, hi=hi, hi_strict=hs,
                        unit_code=unit[0] if unit else None, unit_text=unit[1] if unit else None)
            for lo, ls, hi, hs in ranges]
    return Characteristic(code=code, name=name, type="2", kind=kind, is_required=required,
                          choice_type="2", alternatives=alts)


def position(code, chars, blocked=False, parent="tpl"):
    return Position(code=code, name="тест " + code, is_template=False, status="ACTIVE",
                    has_characteristics=bool(chars), resolve_blocked=blocked,
                    resolve_blocked_reason=["no_characteristics"] if blocked else None,
                    parent_code=parent, application_date_start=None, no_new_features=None,
                    category=None, characteristics=chars)


class FakeIndex:
    version = "test"

    def __init__(self, positions):
        self.by_code = {p.code: p for p in positions}
        self.families = {}

    def pool_for(self, codes):
        return [self.by_code[c] for c in codes if c in self.by_code]

    def family_pool(self, template):
        return [p for p in self.by_code.values() if p.parent_code == template]


UNIT = {"code": "003", "text": "Миллиметр"}
WATT = {"code": "212", "text": "Ватт"}


class TestAlgorithmC(unittest.TestCase):
    def setUp(self):
        self.units = Units()
        self.net = position("P-1", [char_text("Тип питания", "d1", ["От сети"]),
                                    char_range("Мощность", "d2", [(500, False, 750, True)],
                                               unit=(WATT["code"], WATT["text"]))])
        self.acc = position("P-2", [char_text("Тип питания", "d1", ["От аккумулятора"])])
        self.index = FakeIndex([self.net, self.acc])

    def test_value_splits_pool(self):
        from .contract import Unit
        res = run_c([self.net, self.acc], [Fact(kind="text", value="От сети", name="Тип питания"),
                    Fact(kind="number", value=600, name="Мощность", unit=Unit(**WATT))],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.survivors, ["P-1"])
        self.assertTrue(res.flags.ktru_unique)

    def test_missing_property_is_not_exclusion(self):
        res = run_c([self.net, self.acc], [Fact(kind="number", value=5, name="Несуществующее")],
                    self.index, self.units, type_verified=True)
        self.assertEqual(len(res.survivors), 2)
        self.assertNotIn("conflicting_facts", res.flags.blocking)

    def test_strict_boundary(self):
        from .contract import Unit
        res = run_c([self.net], [Fact(kind="number", value=750, name="Мощность", unit=Unit(**WATT))],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "incompatible")

    def test_value_inside_range(self):
        from .contract import Unit
        res = run_c([self.net], [Fact(kind="text", value="От сети", name="Тип питания"),
                    Fact(kind="number", value=600, name="Мощность", unit=Unit(**WATT))],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "compatible")

    def test_number_without_unit_is_unknown(self):
        res = run_c([self.net], [Fact(kind="number", value=600, name="Мощность")],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.candidates[0].unresolved[0].reason, "user_unit_missing")

    def test_units_converted(self):
        from .contract import Unit
        res = run_c([self.net], [Fact(kind="text", value="От сети", name="Тип питания"),
                    Fact(kind="number", value=0.6, name="Мощность",
                                      unit=Unit(code="2355", text="кВт"))],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "compatible")   # 0,6 кВт = 600 Вт

    def test_wrong_dimension_is_unknown_not_exclusion(self):
        from .contract import Unit
        res = run_c([self.net], [Fact(kind="number", value=5, name="Мощность",
                                      unit=Unit(code="166", text="кг"))],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "unknown")
        self.assertEqual(res.candidates[0].unresolved[0].reason, "unit_dimension_unsupported")

    def test_conflicting_facts_block_resolution(self):
        res = run_c([self.net, self.acc],
                    [Fact(kind="text", value="От сети", name="Тип питания", def_id="d1"),
                     Fact(kind="text", value="От аккумулятора", name="Тип питания", def_id="d1")],
                    self.index, self.units, type_verified=True)
        self.assertIn("conflicting_facts", res.flags.blocking)
        self.assertIsNone(res.selected_ktru_code)

    def test_low_confidence_is_not_hard_filter(self):
        res = run_c([self.net, self.acc],
                    [Fact(kind="text", value="От сети", name="Тип питания", extraction_confidence=0.4,
                          fact_id="soft-1")],
                    self.index, self.units, type_verified=True)
        self.assertEqual(len(res.survivors), 2)          # мягкий факт не исключает
        second = next(c for c in res.candidates if c.code == "P-2")
        self.assertEqual(second.unresolved[0].reason, "uncertain_or_low_confidence_fact")
        self.assertEqual(second.verdict, "unknown")
        self.assertFalse(res.flags.value_verified)

    def test_blocked_position_counted_but_retained_as_unknown_competitor(self):
        blocked = position("P-3", [], blocked=True)
        res = run_c([self.net, blocked], [Fact(kind="text", value="От сети", name="Тип питания")],
                    FakeIndex([self.net, blocked]), self.units, type_verified=True)
        self.assertEqual(res.counts["blocked_excluded"], 1)
        self.assertEqual(set(res.survivors), {"P-1", "P-3"})
        self.assertEqual(next(c for c in res.candidates if c.code == "P-3").verdict, "unknown")
        self.assertFalse(res.flags.ktru_unique)
        self.assertIsNone(res.selected_ktru_code)

    def test_no_resolution_without_type_verified(self):
        res = run_c([self.net, self.acc], [Fact(kind="text", value="От сети", name="Тип питания")],
                    self.index, self.units, type_verified=False)
        self.assertIsNone(res.selected_ktru_code)

    def test_same_name_different_values_is_not_conflict(self):
        """Разные свойства, совпавшие по имени, не должны блокировать разрешение."""
        from .contract import Unit
        first = position("P-4", [char_text("Вид", "v1", ["А"]), char_range("Длина", "l1", [(1, False, 2, False)])])
        res = run_c([first], [Fact(kind="text", value="А", name="Вид", def_id="v1"),
                              Fact(kind="number", value=1.5, name="Длина", def_id="l1", unit=Unit(**UNIT))],
                    FakeIndex([first]), self.units, type_verified=True)
        self.assertNotIn("conflicting_facts", res.flags.blocking)

    def test_ambiguous_name_in_position_is_not_exclusion(self):
        """Одно имя — две разные характеристики: подставлять произвольное правило нельзя."""
        twin = position("P-5", [char_text("Объем", "o1", ["Малый"]),
                                char_text("Объем", "o2", ["Большой"])])
        res = run_c([twin], [Fact(kind="text", value="Малый", name="Объем", extraction_confidence=0.9)],
                    FakeIndex([twin]), self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "unknown")
        self.assertEqual(res.candidates[0].unresolved[0].reason, "name_ambiguous_in_position")
        self.assertFalse(res.candidates[0].exclusions)

    def test_fact_with_foreign_def_id_is_matched_by_name_when_unambiguous(self):
        """def_id из другой позиции: если имя в позиции однозначно, проверяем по нему."""
        other = position("P-6", [char_text("Тип питания", "x9", ["От сети"])])
        res = run_c([self.acc], [Fact(kind="text", value="От сети", name="Тип питания",
                                      def_id="x9", extraction_confidence=0.9)],
                    FakeIndex([self.acc, other]), self.units, type_verified=True)
        # у P-2 допустимо только «От аккумулятора», поэтому исключение — верный вердикт
        self.assertEqual(res.candidates[0].verdict, "incompatible")
        self.assertTrue(res.candidates[0].exclusions)

    def test_fact_with_unknown_property_never_excludes(self):
        res = run_c([self.acc], [Fact(kind="text", value="От сети", name="Совершенно другое свойство",
                                      def_id="x9", extraction_confidence=0.9)],
                    FakeIndex([self.acc]), self.units, type_verified=True)
        self.assertEqual(res.candidates[0].verdict, "unknown")
        self.assertFalse(res.candidates[0].exclusions)
        self.assertEqual(res.candidates[0].unresolved[0].reason, "not_defined_in_position")


class TestAlgorithmD(unittest.TestCase):
    def setUp(self):
        self.units = Units()
        self.a = position("A", [char_text("Тип", "d1", ["Ключевой"])])
        self.b = position("B", [char_text("Тип", "d1", ["Быстрозажимной"])])
        self.index = FakeIndex([self.a, self.b])

    def test_question_splits_two(self):
        q = next_question([self.a, self.b], set(), self.index, type_verified=True)
        self.assertIsNone(q.stop_reason)
        self.assertEqual(q.worst_case, 1)

    def test_no_repeat(self):
        q = next_question([self.a, self.b], {"d1"}, self.index, type_verified=True)
        self.assertIsNotNone(q.stop_reason)

    def test_type_not_verified_asks_category(self):
        q = next_question([self.a, self.b], set(), self.index, type_verified=False)
        self.assertEqual(q.reason, "confirms_type")

    def test_one_candidate_stops(self):
        q = next_question([self.a], {"d1"}, self.index, type_verified=True)
        self.assertEqual(q.stop_reason, "already_unique")

    def test_indistinguishable_stops(self):
        x = position("X", [char_range("Длина", "d9", [(1, False, 2, False)])])
        y = position("Y", [char_range("Длина", "d9", [(1, False, 2, False)])])
        q = next_question([x, y], set(), FakeIndex([x, y]), type_verified=True)
        self.assertEqual(q.stop_reason, "value_indistinguishable")

    def test_blocking_returns_reason_not_question(self):
        q = next_question([self.a, self.b], set(), self.index, type_verified=True,
                          blocking_issues=["conflicting_facts"])
        self.assertEqual(q.reason, "fixes_conflict")
        self.assertIn("conflicting_facts", q.stop_reason)

    def test_property_missing_somewhere_still_askable(self):
        """Свойство есть у части кандидатов: вопрос допустим, если он сужает оставшихся."""
        c = position("C", [char_text("Тип", "d1", ["Ключевой"])])
        d = position("D", [char_text("Тип", "d1", ["Быстрозажимной"])])
        q = next_question([self.a, c, d], set(), FakeIndex([self.a, c, d]), type_verified=True)
        self.assertIsNone(q.stop_reason)

    def test_answered_keys_do_not_confirm_alternative_definitions(self):
        f = Fact(kind="text", value="x", def_id="a", name="Имя",
                 definition_alternatives=[{"def_id": "b"}])
        keys = answered_from_facts([f])
        self.assertIn("a", keys)
        self.assertNotIn("b", keys)


class TestFixtureCorpus(unittest.TestCase):
    """Проверки на мини-справочнике из samples/ — том же, что используется в validation_run."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(SAMPLES, "ktru_mini_index.json")
        cls.index = KtruIndex.load_json(path) if os.path.exists(path) else None
        cls.units = Units.load() if cls.index else None

    def setUp(self):
        if self.index is None:
            self.skipTest("нет samples/ktru_mini_index.json")

    def test_drill_family_splits_by_power_type(self):
        pool = self.index.family_pool(DRILL_TEMPLATE)
        self.assertEqual(len(pool), 2)
        res = run_c(pool, [Fact(kind="text", value="От сети", name="Тип питания")],
                    self.index, self.units, type_verified=True)
        self.assertEqual(res.survivors, ["28.24.11.000-00000001"])

    def test_same_structure_family_stops_dialogue(self):
        pool = self.index.family_pool(SAME_STRUCTURE_TEMPLATE)
        self.assertGreater(len(pool), 1)
        q = next_question(pool, set(), self.index, type_verified=True)
        self.assertEqual(q.stop_reason, "value_indistinguishable")

    def test_heavy_family_needs_several_questions(self):
        heavy = max(self.index.families.items(), key=lambda kv: len(kv[1]))
        pool = heavy[1]
        self.assertGreater(len(pool), 50)
        q = next_question(pool, set(), self.index, type_verified=True)
        self.assertIsNone(q.stop_reason)
        self.assertLess(q.worst_case, len(pool))

    def test_stage_model_sample_runs_end_to_end(self):
        path = os.path.join(SAMPLES, "stage_model_output.json")
        if not os.path.exists(path):
            self.skipTest("нет samples/stage_model_output.json")
        with open(path, encoding="utf-8") as fh:
            sample = json.load(fh)
        checked = 0
        for item in sample["items"]:
            pool = self.index.pool_for(item["pool"])
            facts = [Fact.from_dict(f) for f in item["facts"]]
            res = run_c(pool, facts, self.index, self.units, type_verified=True)
            self.assertTrue(res.candidates, "у товара %s нет вердиктов" % item["position_code"])
            checked += 1
        self.assertGreater(checked, 0)

    def test_string_bridge_is_not_hard_filter(self):
        f = fact_from_string("Мощность: 0,65 кВт")
        self.assertEqual(f.kind, "number")
        self.assertFalse(f.is_hard)


if __name__ == "__main__":
    unittest.main(verbosity=2)
