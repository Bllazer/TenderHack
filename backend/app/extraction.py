"""Conservative extraction: raw offsets remain attached, no inferred missing values."""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from threading import Lock

from app.schemas import Fact, Fragment, InputFact
from app.search import canon
from app.units import UNITS

NUMBER = r"[+−-]?\d+(?:[.,]\d+)?"
UNIT_PATTERN = "|".join(re.escape(alias) for alias in sorted(UNITS, key=len, reverse=True))
BRACKET_INTERVAL = rf"[\[(]\s*{NUMBER}\s*;\s*{NUMBER}\s*[\])]"
MEASUREMENT = re.compile(rf"(?P<value>{BRACKET_INTERVAL}|(?:(?:не менее|не более|не выше|не ниже|[<>]=?|[≤≥])\s*)?{NUMBER}(?:\s*(?:–|\.\.|…|-)\s*{NUMBER})?)\s*(?P<unit>{UNIT_PATTERN})?(?=$|[\s,;.)\]])", re.I)

#: Та же токенизация, что внутри `app.search.Analyzer.tokens`: идентификаторы,
#: латиница, числа и единицы не трогаются, лемматизируется только кириллица.
TOKEN = re.compile(r"[\w²³]+(?:[-·/][\w²³]+)*")

#: «0, 75» — неоднозначный разделитель: цифра, запятая, пробел(ы), цифра.
#: Это либо потерянное десятичное «0,75», либо два числа списка; угадывать нельзя.
AMBIGUOUS_SEPARATOR = re.compile(r"\d,\s+\d")
AMBIGUOUS_NUMBER = re.compile(rf"^(?P<value>{NUMBER}\s*,\s+{NUMBER})\s*(?P<unit>{UNIT_PATTERN})?$", re.I)

#: Число и следующее за ним слово, которого нет в справочнике единиц: «5 футов».
UNSUPPORTED_UNIT = r"[^\s,;:.()\[\]]+"
UNSUPPORTED_MEASUREMENT = re.compile(rf"^(?P<value>{NUMBER})\s+(?P<unit>{UNSUPPORTED_UNIT})$")

#: Кусок после запятой, начинающийся с шаблона «имя:», открывает новый факт.
EXPLICIT_PAIR = re.compile(r"^\s*[^:=]{1,120}\s*[:=]")

_ANALYZER = None
_ANALYZER_LOCK = Lock()


def analyzer():
    """Модульный синглтон `Analyzer`: `pymorphy3.MorphAnalyzer` дорог в создании.

    Ленивая инициализация: без имён свойств (пустой справочник) морфология вообще
    не поднимается.
    """
    global _ANALYZER
    if _ANALYZER is None:
        from app.search import Analyzer
        with _ANALYZER_LOCK:
            if _ANALYZER is None:
                _ANALYZER = Analyzer()
    return _ANALYZER


def lemmas(text: str) -> tuple[str, ...]:
    """Леммы токенов тем же анализатором, что и `app.search.Analyzer.tokens`."""
    return tuple(analyzer().lemma(canon(match.group(0))) for match in TOKEN.finditer(text))


def token_spans(text: str) -> list[tuple[str, int, int]]:
    """(лемма, начало, конец) для каждого токена — смещения нужны для span."""
    return [(analyzer().lemma(canon(match.group(0))), match.start(), match.end())
            for match in TOKEN.finditer(text)]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def decimal_comma(text: str, index: int) -> bool:
    """Запятая между цифрами — десятичный разделитель, а не граница факта."""
    left = text[index - 1] if index else ""
    right = text[index + 1] if index + 1 < len(text) else ""
    return left.isdigit() and right.isdigit()


def ambiguous_separator(text: str, index: int) -> bool:
    """«0, 75»: цифра, запятая, пробел(ы), цифра — ни десятичное, ни граница факта.

    Такой разделитель нельзя разрешить молча: «0, 75 м» превращалось в
    подтверждённый ноль и осиротевшее «75 м».
    """
    left = text[index - 1] if index else ""
    return left.isdigit() and re.match(r"\s+\d", text[index + 1:]) is not None


def name_dictionary(names: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Пары «имя свойства — леммы имени», длинные имена первыми.

    Сопоставление идёт по последовательности лемм, а не по подстроке: «мощностью»
    находит «Мощность», а «Мошность» — нет (похожесть ничего не «исправляет»).
    Набор имён стабилен для снимка каталога, поэтому леммы считаются один раз.
    """
    return _name_dictionary(names)


@lru_cache(maxsize=4)
def _name_dictionary(names: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((name, lemmas(name)) for name in sorted(set(names), key=len, reverse=True))


def canonical_name(surface: str, dictionary) -> str:
    """Имя свойства из справочника, если написание подтверждено леммами.

    «мощностью» → «Мощность»: форма слова проверена тем же анализатором, что и
    весь разбор, и это не «починка по похожести». «Мошность» остаётся
    «Мошностью»: леммы не совпали, и подменять написание нельзя — потенциально
    значимая оправка требует подтверждения пользователем.
    """
    wanted = lemmas(surface or "")
    if not wanted:
        return surface
    hits = [name for name, entry in dictionary if entry == wanted]
    return hits[0] if len(hits) == 1 else surface


def starts_with_property_name(text: str, dictionary) -> bool:
    if not dictionary:
        return False
    head = lemmas(text)
    return any(wanted and head[:len(wanted)] == wanted for _, wanted in dictionary)


def is_property_name(word: str, dictionary) -> bool:
    """Слово — известное имя свойства (тогда это не неподдержанная единица)."""
    if not dictionary:
        return False
    wanted = lemmas(word)
    return bool(wanted) and any(wanted == lemma_name for _, lemma_name in dictionary)


def starts_new_fact(text: str, position: int, dictionary) -> bool:
    """Начинается ли после запятой новый факт: «имя:» или известное имя свойства.

    Иначе запятая — разделитель значений одного факта: «Цвет: красный, белый».
    """
    rest = text[position:]
    cut = re.search(r"[\n|;,]", rest)
    chunk = rest[:cut.start()] if cut else rest
    if EXPLICIT_PAIR.match(chunk):
        return True
    return starts_with_property_name(chunk, dictionary)


def segments(text: str, dictionary=()):
    start, depth = 0, 0
    for index, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        if char in "\n|;" and depth == 0:
            yield start, index, text[start:index]
            start = index + 1
        elif char == "," and depth == 0 and not decimal_comma(text, index) and not ambiguous_separator(text, index):
            # «Мощность: 650 Вт, напряжение 220 В» — два факта (второй кусок начинает
            # новый факт). «Цвет: красный, белый» — один факт: «белый» не теряется.
            if starts_new_fact(text, index + 1, dictionary):
                yield start, index, text[start:index]
                start = index + 1
    yield start, len(text), text[start:]


def make_fact(name: str, value: str, unit: str | None, raw: str, span: tuple[int, int], index: int,
              negated: bool = False, unknown: bool = False) -> Fact:
    return Fact(id=f"text:{span[0]}:{span[1]}:{index}", name=name.strip(), value=value.strip(), unit=unit,
                raw=raw, span=span, source="text", negated=negated,
                subject="packaging" if "упаковк" in canon(name) else "product",
                unknown=unknown or canon(value) in {"", "не знаю", "не задано", "(не задано)"})


def measurement_value(value: str) -> str:
    return re.sub(r"\s+", "", value) if re.fullmatch(BRACKET_INTERVAL, value) else value


def split_measurement(value: str, dictionary=()) -> tuple[str, str | None, bool]:
    """Значение, единица и признак «значение не подтверждено».

    Признак ставится для неоднозначного разделителя («0, 75»): такой факт
    сохраняет текст как в описании и уходит на уточнение. Неподдержанная единица
    («5 футов») признаком не помечается: её видно по самой единице, которую
    справочник не знает, и решение принимает стадия структуры.
    """
    text = value.strip()
    ambiguous = AMBIGUOUS_NUMBER.fullmatch(text)
    if ambiguous:
        return ambiguous["value"].strip(), ambiguous["unit"], True
    match = MEASUREMENT.fullmatch(text)
    if match:
        return measurement_value(match["value"]), match["unit"], False
    if AMBIGUOUS_SEPARATOR.search(text):
        # «0, 75 м» с неподдержанным или нераспознанным хвостом: текст сохраняется.
        return text, None, True
    unsupported = UNSUPPORTED_MEASUREMENT.fullmatch(text)
    if unsupported and not is_property_name(unsupported["unit"], dictionary):
        return unsupported["value"], unsupported["unit"], False
    return text, None, False


@lru_cache(maxsize=128)
def enum_dictionary(properties):
    """Reusable vocabulary of immutable scope rules, not a second search index."""
    return tuple((prop, item.value, canon(item.value)) for prop in properties
                 if prop.verified and prop.value_type in ("text", "boolean")
                 for item in prop.allowed if item.kind == "enum" and item.value)


def enum_targets(properties, value: str, name: str = "", subject: str | None = None):
    """Scope-only suggestions; similarity is never verification."""
    exact, close = {}, {}
    supplied = canon(value)
    for prop, target, wanted in enum_dictionary(tuple(properties)):
        if subject and prop.subject != subject:
            continue
        if name and canon(name) not in {canon(prop.name), *(canon(alias) for alias in prop.aliases)}:
            continue
        key = (prop.semantic_key or prop.id, prop.subject, prop.name, target)
        if wanted == supplied:
            exact[key] = (prop, target)
        elif len(supplied) >= 5 and len(wanted) >= 5 and SequenceMatcher(None, supplied, wanted).ratio() >= .85:
            close[key] = (prop, target)
    return list(exact.values()), list(close.values())


def extract(text: str, names: tuple[str, ...], *, scoped_properties=()) -> tuple[list[Fact], list[Fragment]]:
    facts: list[Fact] = []
    covered: list[tuple[int, int]] = []
    dictionary = name_dictionary(names)
    for start, end, segment in segments(text, dictionary):
        pair = re.match(r"\s*([^:=\n]{1,120})\s*[:=]\s*(.+?)\s*$", segment)
        if pair:
            value, unit, unconfirmed = split_measurement(pair[2], dictionary)
            # Имя из справочника, если форма слова подтверждена леммами; иначе — как в тексте.
            facts.append(make_fact(canonical_name(pair[1], dictionary), value, unit, segment,
                                   (start, end), len(facts), unknown=unconfirmed))
            covered.append((start, end))
            continue
        # Only explicit property phrases from vetted dictionaries, longest first.
        # Без имён свойств морфология не поднимается вовсе (ленивый синглтон).
        tokens = token_spans(segment) if dictionary else []
        for name, wanted in dictionary:
            if not wanted:
                continue
            for index in range(len(tokens) - len(wanted) + 1):
                window = tokens[index:index + len(wanted)]
                if tuple(item[0] for item in window) != wanted:
                    continue
                left_token, right_token = window[0][1], window[-1][2]
                absolute = start + left_token
                if any(begin <= absolute < finish for begin, finish in covered):
                    continue
                boundary = re.match(r"\s*(?:[:=]\s*)?", segment[right_token:])
                match_end = right_token + boundary.end()
                matched = segment[left_token:match_end]
                prefix = segment[max(0, left_token - 4):left_token]
                if re.search(r"без\s*$", prefix, re.I):
                    left, right = start + max(0, left_token - 4), start + match_end
                    facts.append(make_fact(name, "нет", None, text[left:right], (left, right),
                                           len(facts), negated=True))
                    covered.append((left, right))
                    continue
                tail = segment[match_end:]
                measure = MEASUREMENT.match(tail)
                if measure:
                    unit, consumed = measure["unit"], measure.end()
                    if not unit:
                        # «5 футов»: слово после числа — не единица справочника и не
                        # имя свойства. Сохраняем написание, но не подтверждаем факт.
                        unsupported = re.match(rf"\s+(?P<unit>{UNSUPPORTED_UNIT})", tail[consumed:])
                        if unsupported and not is_property_name(unsupported["unit"], dictionary):
                            unit, consumed = unsupported["unit"], consumed + unsupported.end()
                    right = start + match_end + consumed
                    facts.append(make_fact(name, measurement_value(measure["value"]), unit,
                                           text[absolute:right], (absolute, right), len(facts)))
                    covered.append((absolute, right))
                else:
                    value = tail.strip()
                    if value:
                        lead = len(tail) - len(tail.lstrip())
                        right = start + match_end + lead + len(value)
                        facts.append(make_fact(name, value, None, text[absolute:right],
                                               (absolute, right), len(facts)))
                        covered.append((absolute, right))
    # Reuse A's segmentation/provenance and the confirmed scope, never the global catalog.
    values = sorted({value for _, value, _ in enum_dictionary(tuple(scoped_properties))}, key=len, reverse=True)
    windows = []
    tokens = list(re.finditer(r"\w+", text))
    clauses = list(segments(text, dictionary))
    for value in values:
        words = re.findall(r"\w+", value)
        if not words:
            continue
        pattern = r"(?<!\w)" + re.escape(value) + r"(?!\w)"
        windows.extend((m.start(), m.end()) for m in re.finditer(pattern, text, re.I))
        # Only complete token windows; the correction flow requires explicit acceptance.
        for i in range(len(tokens) - len(words) + 1):
            left, right = tokens[i].start(), tokens[i + len(words) - 1].end()
            supplied = canon(text[left:right])
            if len(supplied) >= 5 and len(value) >= 5 and SequenceMatcher(None, supplied, canon(value)).ratio() >= .85:
                windows.append((left, right))
    for left, right in sorted(set(windows), key=lambda span: (span[0], -(span[1] - span[0]))):
        if any(left < end and right > start for start, end in covered):
            continue
        value = text[left:right]
        exact, close = enum_targets(scoped_properties, value)
        ownership = {(prop.semantic_key or prop.id, prop.subject) for prop, _ in exact}
        begin, end, clause = next((a, b, chunk) for a, b, chunk in clauses if a <= left and right <= b)
        negation = bool(re.search(r"\b(?:не|без|нет)\b", clause, re.I))
        uncertain = negation or "?" in clause or bool(re.search(
            r"\b(?:возможно|вероятно|кажется|предположительно|неизвестно|непонятно)\b", clause, re.I))
        raw_left, raw_right = (begin, end) if uncertain else (left, right)
        if len(ownership) == 1:
            prop = exact[0][0]
            fact = make_fact(prop.name, value, None, text[raw_left:raw_right], (raw_left, raw_right), len(facts),
                             negated=negation, unknown=uncertain)
            fact.subject = prop.subject
        elif exact or close:
            fact = make_fact("", value, None, text[raw_left:raw_right], (raw_left, raw_right), len(facts),
                             negated=negation, unknown=uncertain)
        else:
            continue
        facts.append(fact)
        covered.append((left, right))
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
