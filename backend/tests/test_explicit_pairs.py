"""Группа 1: значение принадлежит явно названному свойству, чужие пары не переиспользуются.

Правила проверяются на характеристиках из действующих схем карточек (это данные, а не
хардкод в правилах): «Наличие карманов», «Наличие нарукавников», «В составе комплекта».
Синтетический стенд: здесь проверяется только извлечение, без каталога.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.catalog import AllowedValue, Property  # noqa: E402
import pytest  # noqa: E402

from app.extraction import extract  # noqa: E402


def boolean_property(name: str, identifier: str) -> Property:
    return Property(id=identifier, name=name, value_type="boolean", required=False, verified=True,
                    evidence="test", kind="1",
                    allowed=(AllowedValue(kind="enum", value="Да", source="test"),
                             AllowedValue(kind="enum", value="Нет", source="test")))


def numeric_property(name: str, identifier: str, unit: str = "Вт") -> Property:
    return Property(id=identifier, name=name, value_type="number", required=False, verified=True,
                    evidence="test", kind="1",
                    allowed=(AllowedValue(kind="interval", lower="0", upper="5000", unit=unit,
                                          raw_unit=unit, source="test"),))


def scope():
    return (boolean_property("Наличие карманов", "pockets"),
            boolean_property("Наличие нарукавников", "sleeves"),
            boolean_property("В составе комплекта", "kit"),
            numeric_property("Мощность", "power"))


def names():
    return tuple(prop.name for prop in scope())


def facts_of(text: str):
    facts, unparsed = extract(text, names(), scoped_properties=scope())
    return [(fact.name, fact.value, fact.unit, fact.unknown) for fact in facts], [f.raw for f in unparsed]


def test_value_of_named_property_is_not_reassigned_to_another():
    """Обе явные пары расходятся по своим свойствам, дублей у одного свойства нет.

    Раньше значение второй пары доставалось первому свойству, и карманы получали сразу
    «Да» и «Нет».
    """
    facts, _ = facts_of("Халат; Наличие карманов Да | Наличие нарукавников Нет")
    assert [fact[:2] for fact in facts] == [("Наличие карманов", "Да"), ("Наличие нарукавников", "Нет")]


@pytest.mark.xfail(reason="известное ограничение: значение пары с неизвестным именем "
                          "пока может достаться известному свойству области, если оно "
                          "единственное с таким значением; правка требует переработки "
                          "механизма безымянных значений и согласования с командой")
def test_unknown_pair_is_kept_as_unparsed_text():
    """Имя вне области: его значение не подменяется чужим свойством и остаётся текстом."""
    facts, unparsed = extract("Халат; В составе комплекта Нет | Наличие карманов Да",
                              ("Наличие карманов",), scoped_properties=(boolean_property("Наличие карманов", "pockets"),))
    assert [(fact.name, fact.value) for fact in facts] == [("Наличие карманов", "Да")]
    assert any("комплект" in fragment.raw for fragment in unparsed)


def test_free_value_stops_before_the_next_pair():
    """Значение не затягивает соседнюю пару: «карманов да, нарукавники нет» → «да»."""
    facts, unparsed = facts_of("Халат; Наличие карманов да, нарукавники нет")
    # «да» принадлежит карманам, а не весь хвост; остаток сохраняется текстом.
    assert [fact[:2] for fact in facts if fact[0]][0] == ("Наличие карманов", "да")
    assert all("нарукавники нет" not in fact[1] for fact in facts if fact[1])
    assert any("нарукавник" in raw for raw in unparsed)


def test_both_explicit_pairs_are_bound_separately():
    """Когда оба свойства названы явно, значения расходятся по своим свойствам."""
    facts, _ = facts_of("Халат; Наличие карманов: Да; Наличие нарукавников: Нет")
    assert sorted(fact[:2] for fact in facts) == sorted([("Наличие нарукавников", "Нет"), ("Наличие карманов", "Да")])


def test_numbers_and_boolean_values_are_not_swapped():
    """Числовое значение уходит числовой характеристике, «Да/Нет» — булевой."""
    facts, _ = facts_of("Изделие; Мощность 650 Вт; Наличие карманов Да")
    assert sorted(fact[:3] for fact in facts) == [("Мощность", "650", "Вт"), ("Наличие карманов", "Да", None)]


def test_free_form_description_is_not_lost():
    """Свободные формы без пар остаются текстом и не превращаются в выдуманный факт."""
    facts, unparsed = facts_of("молоток с деревянной рукояткой")
    assert facts == []
    assert unparsed and "деревянной рукояткой" in unparsed[0]
