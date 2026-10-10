"""Conservative extraction: raw offsets remain attached, no inferred missing values."""
import re
import unicodedata

from app.schemas import Fact, Fragment, InputFact
from app.search import canon
from app.units import UNITS

NUMBER = r"[+−-]?\d+(?:[.,]\d+)?"
UNIT_PATTERN = "|".join(re.escape(alias) for alias in sorted(UNITS, key=len, reverse=True))
BRACKET_INTERVAL = rf"[\[(]\s*{NUMBER}\s*;\s*{NUMBER}\s*[\])]"
MEASUREMENT = re.compile(rf"(?P<value>{BRACKET_INTERVAL}|(?:(?:не менее|не более|не выше|не ниже|[<>]=?|[≤≥])\s*)?{NUMBER}(?:\s*(?:–|\.\.|…|-)\s*{NUMBER})?)\s*(?P<unit>{UNIT_PATTERN})?(?=$|[\s,;.)\]])", re.I)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def decimal_comma(text: str, index: int) -> bool:
    """Запятая между цифрами — десятичный разделитель, а не граница факта."""
    left = text[index - 1] if index else ""
    right = text[index + 1] if index + 1 < len(text) else ""
    return left.isdigit() and right.isdigit()


def segments(text: str):
    start, depth = 0, 0
    for index, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if char in "\n|;" and depth == 0:
            yield start, index, text[start:index]
            start = index + 1
        elif char == "," and depth == 0 and not decimal_comma(text, index):
            # Запятая разделяет факты: «Мощность: 650 Вт, напряжение 220 В» — это два
            # факта, а не один со значением до конца строки. Без этого двоеточие
            # забирало весь остаток сегмента в значение первого свойства.
            yield start, index, text[start:index]
            start = index + 1
    yield start, len(text), text[start:]


def make_fact(name: str, value: str, unit: str | None, raw: str, span: tuple[int, int], index: int,
              negated: bool = False) -> Fact:
    return Fact(id=f"text:{span[0]}:{span[1]}:{index}", name=name.strip(), value=value.strip(), unit=unit,
                raw=raw, span=span, source="text", negated=negated,
                subject="packaging" if "упаковк" in canon(name) else "product",
                unknown=canon(value) in {"", "не знаю", "не задано", "(не задано)"})


def split_measurement(value: str) -> tuple[str, str | None]:
    match = MEASUREMENT.fullmatch(value.strip())
    return (measurement_value(match["value"]), match["unit"]) if match else (value.strip(), None)


def measurement_value(value: str) -> str:
    return re.sub(r"\s+", "", value) if re.fullmatch(BRACKET_INTERVAL, value) else value


def extract(text: str, names: tuple[str, ...]) -> tuple[list[Fact], list[Fragment]]:
    facts: list[Fact] = []
    covered: list[tuple[int, int]] = []
    for start, end, segment in segments(text):
        pair = re.match(r"\s*([^:=\n]{1,120})\s*[:=]\s*(.+?)\s*$", segment)
        if pair:
            value, unit = split_measurement(pair[2])
            facts.append(make_fact(pair[1], value, unit, segment, (start, end), len(facts)))
            covered.append((start, end))
            continue
        # Only explicit property phrases from vetted dictionaries, longest first.
        for name in sorted(set(names), key=len, reverse=True):
            pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)\s*(?:[:=]\s*)?", re.I)
            for match in pattern.finditer(segment):
                absolute = start + match.start()
                if any(left <= absolute < right for left, right in covered):
                    continue
                prefix = segment[max(0, match.start() - 4):match.start()]
                if re.search(r"без\s*$", prefix, re.I):
                    left, right = start + max(0, match.start() - 4), start + match.end()
                    facts.append(make_fact(match[0].strip(), "нет", None, text[left:right], (left, right), len(facts), True))
                    covered.append((left, right))
                    continue
                tail = segment[match.end():]
                measure = MEASUREMENT.match(tail)
                if measure:
                    right = start + match.end() + measure.end()
                    facts.append(make_fact(match[0].strip(" :="), measurement_value(measure["value"]), measure["unit"],
                                           text[absolute:right], (absolute, right), len(facts)))
                    covered.append((absolute, right))
                else:
                    value_match = re.match(r"([^,;]+)", tail)
                    if value_match and value_match[0].strip():
                        right = start + match.end() + value_match.end()
                        facts.append(make_fact(match[0].strip(" :="), value_match[0], None,
                                               text[absolute:right], (absolute, right), len(facts)))
                        covered.append((absolute, right))
    unparsed: list[Fragment] = []
    cursor = 0
    for start, end in sorted(covered):
        if start > cursor and text[cursor:start].strip(" \n|;,."):
            unparsed.append(Fragment(id=f"fragment:{cursor}:{start}", raw=text[cursor:start], span=(cursor, start)))
        cursor = max(cursor, end)
    if text[cursor:].strip(" \n|;,."):
        unparsed.append(Fragment(id=f"fragment:{cursor}:{len(text)}", raw=text[cursor:], span=(cursor, len(text))))
    return facts, unparsed


def apply_inputs(extracted: list[Fact], inputs: list[InputFact]) -> list[Fact]:
    result = list(extracted)
    for item in inputs:
        if item.replaces:
            result = [fact for fact in result if fact.id != item.replaces]
        result.append(Fact(id=item.id, name=item.name, value=item.value, unit=item.unit, raw=item.value,
                           source="input", unknown=item.unknown or not item.value.strip(), subject=item.subject))
    return result
