"""Conservative extraction: raw offsets remain attached, no inferred missing values."""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from threading import Lock

from app.schemas import Correction, Fact, FactAssignment, Fragment, InputFact
from app.search import canon
from app.units import UNITS, get_unit

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
        if wanted == supplied or lemmas(wanted) == lemmas(supplied):
            exact[key] = (prop, target)
        elif len(supplied) >= 5 and len(wanted) >= 5 and SequenceMatcher(None, supplied, wanted).ratio() >= .85:
            close[key] = (prop, target)
    return list(exact.values()), list(close.values())


def local_uncertainty(text, left, right, begin, end):
    """A negation attaches to its own nearby target, not every fact in a clause."""
    before, after = text[begin:left], text[right:end]
    negative = bool(re.search(r'\b(?:не(?:\s+(?:совсем|только))?|без|нет)\s*$', before, re.I) or
                    re.match(r'\s+не\s+(?:нуж\w*|треб\w*)', after, re.I))
    uncertain = negative or bool(re.match(r'\s*\?', after)) or bool(re.search(
        r'\b(?:возможно|вероятно|кажется|предположительно|неизвестно|непонятно)\s*$', before, re.I))
    return negative, uncertain


@lru_cache(maxsize=4096)
def part_lemma(word):
    # A schema part label is a noun context. Reuse the existing analyzer, but
    # do not let its first comparative/adjective reading replace that context.
    readings = analyzer().morph.parse(canon(word))
    return next((r.normal_form for r in readings if r.tag.POS == 'NOUN' and r.is_known),
                analyzer().lemma(canon(word)))


def explicit_part(text, left, right, parts):
    tokens = [(part_lemma(m[0]), m.start(), m.end()) for m in TOKEN.finditer(text)]
    for part in parts:
        wanted = tuple(part_lemma(m[0]) for m in TOKEN.finditer(part))
        for i in range(len(tokens) - len(wanted) + 1):
            window = tokens[i:i + len(wanted)]
            if tuple(t[0] for t in window) != wanted:
                continue
            a, b = window[0][1], window[-1][2]
            if (a >= right and not text[right:a].strip() and not re.search(r'[\n;|]', text[right:a])) or (
                    b <= left and re.fullmatch(r'[ \t]+(?:из[ \t]+)?', text[b:left])):
                return True
    return False


def canonicalize_facts(facts, properties, lexicon):
    """The same reviewed morphological mapping for text and explicit card edits."""
    for fact in facts:
        if size_label(fact.name, lexicon) and foreign_size(f'{fact.value} {fact.unit or ""}'):
            target = fact.name
            fact.name, fact.unknown = '', True
            fact.uncertainty_reason = 'Явно указана другая система размера; преобразование в российскую систему не подтверждено'
            fact.assignment_options = [FactAssignment(name=target, value=fact.value, evidence='explicit size-system qualifier')]
        props = [p for p in properties if p.name == fact.name and p.subject == fact.subject]
        if props:
            fact.extraction_evidence = list(dict.fromkeys([*fact.extraction_evidence, *(p.evidence for p in props)]))
        if fact.unknown or fact.negated or fact.values is not None:
            continue
        exact, _ = enum_targets(props, fact.value, fact.name, fact.subject)
        if not exact:
            exact = [(p, target) for p, target, _ in enum_dictionary(tuple(props)) if
                     p.name in lexicon.get('parts', {}) and
                     any(lemmas(fact.value) == lemmas(form) for form in lexicon.get('value_aliases', {}).get(target, []))]
        targets = {v for _, v in exact}
        if len(targets) == 1 and canon(fact.value) != canon(next(iter(targets))):
            fact.surface_value = fact.surface_value or fact.value
            fact.value = next(iter(targets))
            if lexicon:
                fact.extraction_evidence.append(lexicon['evidence'])


SIZE_SYSTEM_WORDS = r'(?:eu|eur|us|usa|uk|int|fr|it|de|jp|cn|european|american|british|international|французск[а-яё]*|итальянск[а-яё]*|немецк[а-яё]*|японск[а-яё]*|китайск[а-яё]*|европейск[а-яё]*|американск[а-яё]*|британск[а-яё]*|международн[а-яё]*|буквен[а-яё]*|xxxs|xxs|xs|s|m|l|xl|xxl|xxxl)'
SIZE_SYSTEM = re.compile(r'\b' + SIZE_SYSTEM_WORDS + r'\b', re.I)


def foreign_size(text):
    return SIZE_SYSTEM.search(text) or re.search(
        r'(?<!\w)(?:eu|eur|us|usa|uk|int|fr|it|de|jp|cn)(?=\d)|(?<=\d)(?:eu|eur|us|usa|uk|int|fr|it|de|jp|cn)(?!\w)', text, re.I)


def size_label(name, lexicon):
    # Reviewed aliases designate the system-specific field; this is not a
    # product/category/code branch. Unknown systems are never converted.
    return ('российский' in lemmas(name) and 'размер' in lemmas(name)) or any(
        lemmas(alias) == ('размер',) for alias in lexicon.get('name_aliases', {}).get(name, ()))


def designation_facts(text, dictionary, properties, lexicon, facts, covered):
    for begin, end, segment in segments(text, dictionary):
        tokens = token_spans(segment)
        for name, wanted in dictionary:
            if not wanted or not size_label(name, lexicon):
                continue
            for i in range(len(tokens) - len(wanted) + 1):
                window = tokens[i:i + len(wanted)]
                if tuple(t[0] for t in window) != wanted:
                    continue
                left, right = window[0][1], window[-1][2]
                before = re.search(rf'(?<![\w.,])(?P<value>{BRACKET_INTERVAL}|{NUMBER}(?:\s*(?:–|\.\.|…|-)\s*{NUMBER})?)(?:-(?:го|й|ый))?\s*$', segment[:left], re.I)
                after = re.match(rf'\s*[:=]?\s*(?P<system>{SIZE_SYSTEM_WORDS}\s+)?(?P<value>{NUMBER})(?![\w.]|,\d)', segment[right:], re.I)
                # An immediately preceding designation owns the label; a later
                # physical measurement is a separate fact, never its value.
                unit = None
                if before:
                    value, left = measurement_value(before['value']), before.start()
                elif after:
                    value_start = right + after.start('value')
                    measure = MEASUREMENT.match(segment[value_start:])
                    value, right = after['value'], right + after.end()
                    if measure:
                        value, unit = measurement_value(measure['value']), measure['unit']
                        right = value_start + measure.end()
                else:
                    letter = re.match(r'\s*[:=]?\s*(?:XXXS|XXS|XS|XXXL|XXL|XL|S|M|L)\b', segment[right:], re.I)
                    if not letter:
                        continue
                    value, right = letter[0].strip(' := '), right + letter.end()
                foreign = foreign_size(segment)
                if foreign:
                    left, right = min(left, foreign.start()), max(right, foreign.end())
                a, b = begin + left, begin + right
                if any(a < y and b > x for x, y in covered):
                    continue
                fact = make_fact('' if foreign else name, value, unit, text[a:b], (a, b), len(facts), unknown=bool(foreign))
                props = [p for p in properties if p.name == name]
                fact.extraction_evidence = list(dict.fromkeys(p.evidence for p in props))
                if foreign:
                    fact.surface_value = text[a:b]
                    fact.uncertainty_reason = 'Уточните систему размера: иностранное или буквенное обозначение не является российским размером'
                    fact.assignment_options = [FactAssignment(name=name, value=value, evidence='explicit size-system qualifier')]
                facts.append(fact)
                covered.append((a, b))
        # A system qualifier itself identifies a designation even without a
        # label ("EU 40"). Do not let a missing label hide meaningful input.
        targets = sorted({name for name, _ in dictionary if size_label(name, lexicon)})
        if targets:
            pattern = rf'\b{SIZE_SYSTEM_WORDS}\s*(?P<after>{NUMBER})(?![\w.]|,\d)|(?<![\w.,])(?P<before>{NUMBER})\s*{SIZE_SYSTEM_WORDS}\b'
            for match in re.finditer(pattern, segment, re.I):
                a, b = begin + match.start(), begin + match.end()
                if any(a < y and b > x for x, y in covered):
                    continue
                fact = make_fact('', match['after'] or match['before'], None, text[a:b], (a, b), len(facts), unknown=True)
                fact.surface_value = fact.raw
                fact.uncertainty_reason = 'Явная система размера отличается от проверенной системы свойства; требуется уточнение без конвертации'
                fact.assignment_options = [FactAssignment(name=name, value=fact.value, evidence='explicit size-system qualifier') for name in targets]
                fact.extraction_evidence = list(dict.fromkeys(p.evidence for p in properties if p.name in targets))
                facts.append(fact)
                covered.append((a, b))


#: Чистое число — измерение, а не часть имени характеристики.
DIGITS = re.compile(r"^[+−-]?\d+$")

#: Между значением и именем допускаются только пробелы и разделители: «650 Вт мощность».
BEFORE_NAME_SEPARATORS = re.compile(r"[\s,;|]*\Z")


def preceding_measurement(text: str, position: int) -> tuple[str, str | None, int] | None:
    """Измерение, стоящее вплотную перед именем: «650 Вт **мощность**».

    В карточках СТЕ порядок всегда «имя значение», но в свободном описании
    значение идёт первым («650 Вт, от сети»), и раньше такой факт терялся целиком.
    Берём только измерение, которое заканчивается ровно перед именем: иначе
    «Мощность 650 Вт напряжение» приписало бы 650 Вт напряжению.
    """
    before = text[:position]
    stripped = before.rstrip()
    if not stripped or not BEFORE_NAME_SEPARATORS.fullmatch(before[len(stripped):]):
        return None
    # Именно измерение, заканчивающееся вплотную перед именем: в строке
    # «2000 мм высота, 600 мм ширина» слева от «ширины» два измерения, нужно последнее.
    match = None
    for candidate in MEASUREMENT.finditer(stripped):
        if candidate.end() == len(stripped):
            match = candidate
    if match is None:
        return None
    unit = match["unit"]
    if not unit:
        word = re.match(rf"\s+(?P<unit>{UNSUPPORTED_UNIT})", stripped[match.end():])
        if word:
            return measurement_value(match["value"]), word["unit"], match.start()
    return measurement_value(match["value"]), unit, match.start()


#: Два порога похожести имени с известным. Замерено на именах характеристик:
#: опечатки дают 0.83…0.95 («Мощьность» 0.941, «маса» 0.889, «напряжене» 0.947),
#: а РАЗНЫЕ имена — не выше 0.57 («Толщина/Глубина» 0.571, «Длина/Ширина» 0.545).
#: Между 0.57 и 0.83 есть запас, пороги стоят в нём.
TYPO_CONFIDENT = 0.85   # выше — имя заменяется автоматически: «Мошность» → «Мощность»
TYPO_SUGGEST = 0.72     # ниже — ничего; между — «возможно, вы имели в виду …»


def fuzzy_name_index(names: tuple[str, ...]) -> dict:
    """Индекс «первая буква и длина → имена» для быстрого поиска опечаток.

    Ключ — первая буква и длина, а не первые две буквы: опечатка бывает во второй
    букве («дакументов» ↔ «документов»), и по двум буквам кандидат не нашёлся бы.
    Без индекса сравнение шло бы со всеми именами (при невыбранной области их
    больше 15 000) на каждое окно токенов.
    """
    index: dict[tuple[str, int], list[str]] = {}
    for name in set(names):
        key = canon(name)
        if key:
            index.setdefault((key[0], len(key)), []).append(name)
    return index


def _candidates(surface: str, names, fuzzy_index) -> list[str]:
    """Имена-кандидаты: та же первая буква, длина ±2, из индекса или из списка."""
    key = canon(surface)
    if not key:
        return []
    if fuzzy_index:
        out: list[str] = []
        for length in range(max(1, len(key) - 2), len(key) + 3):
            out.extend(fuzzy_index.get((key[0], length), ()))
        return out
    return [str(name) for name in names
            if canon(name) and canon(name)[0] == key[0] and abs(len(canon(name)) - len(key)) <= 2]


def closest_name(surface: str, names, fuzzy_index=None) -> tuple[float, str | None]:
    """Наибольшая похожесть написания с именами справочника и само имя."""
    key = canon(surface)
    best: tuple[float, str | None] = (0.0, None)
    for name in _candidates(surface, names, fuzzy_index):
        other = canon(name)
        if not other:
            continue
        ratio = SequenceMatcher(None, key, other).ratio()
        if ratio > best[0]:
            best = (ratio, name)
    return best


def confident_correction(surface: str, names, fuzzy_index=None) -> str | None:
    """Имя из справочника, если написание почти наверняка его опечатка.

    Три условия сразу, и все три — страховка от молчаливой подмены значимого текста:

    * похожесть >= `TYPO_CONFIDENT` (0.85);
    * та же первая буква — опечатка обычно не в начале слова;
    * кандидат **ровно один**: если похожи два имени, автоматика запрещена, и
      написание уходит в подсказку, а не в замену.
    """
    key = canon(surface)
    if len(key) < 5:
        return None
    matches = [name for name in {str(item) for item in _candidates(surface, names, fuzzy_index)}
               if canon(name)[0] == key[0] and abs(len(canon(name)) - len(key)) <= 2
               and SequenceMatcher(None, key, canon(name)).ratio() >= TYPO_CONFIDENT]
    return matches[0] if len(matches) == 1 else None


def typo_hits(segment: str, tokens, dictionary, fuzzy_index, names) -> list[tuple[str, int, int, str | None]]:
    """Окна токенов, похожие на известное имя характеристики, но не совпавшие с ним.

    Возвращает (имя для факта, начало, конец, исходное написание или None).

    Два уровня вместо одного вопроса:

    * похожесть >= `TYPO_CONFIDENT` и кандидат один — **автозамена**: имя факта
      сразу становится каноническим, а исходное написание уходит в провенанс
      (`Correction(confirmation_required=False)`) и остаётся в `raw`;
    * похожесть от `TYPO_SUGGEST` до `TYPO_CONFIDENT` — **подсказка**: факт
      сохраняет написание пользователя, а поток коррекций спрашивает
      «возможно, вы имели в виду …». Ничего не подменяется без ответа.
    """
    if not dictionary or not names:
        return []
    exact = {wanted for _, wanted in dictionary}
    found: list[tuple[str, int, int, str | None]] = []
    for size in range(min(4, len(tokens)), 0, -1):
        for position in range(len(tokens) - size + 1):
            window = tokens[position:position + size]
            if tuple(item[0] for item in window) in exact:
                continue
            # Окно с числом — это имя вместе со значением («Мощность 0» в «Мощность 0,65 кВт»):
            # похожесть на имя высокая, но значение к имени не относится. В именах
            # характеристик цифр не бывает, поэтому такие окна не рассматриваем.
            if any(DIGITS.fullmatch(item[0]) for item in window):
                continue
            left, right = window[0][1], window[-1][2]
            surface = segment[left:right]
            ratio, _ = closest_name(surface, names, fuzzy_index)
            if ratio < TYPO_SUGGEST:
                continue
            corrected = confident_correction(surface, names, fuzzy_index)
            if corrected:
                found.append((corrected, left, right, surface))
            else:
                found.append((surface, left, right, None))
    return found


def partial_hits(segment: str, tokens, dictionary, names) -> list[tuple[str, int, int]]:
    """Окна, леммы которых — строгое подмножество лемм РОВНО ОДНОГО имени характеристики.

    «минимальный диаметр» ⊂ «Минимальный диаметр зажима патрона»: имя написано не
    полностью, но однозначно — это ступень `partial` (мягкий факт), как в модели A.
    «диаметр» подходит и минимуму, и максимуму, поэтому такое окно не берём: значение
    уйдёт на уточнение вопросом, а не будет приписано наугад. Написание сохраняется:
    каноническое имя даёт сопоставление, а не подмена.
    """
    if not names:
        return []
    keys = [(name, tuple(lemmas(name))) for name in set(names)]
    exact = {wanted for _, wanted in dictionary}
    found: list[tuple[str, int, int]] = []
    for size in range(min(4, len(tokens)), 0, -1):
        for position in range(len(tokens) - size + 1):
            window = tokens[position:position + size]
            if tuple(item[0] for item in window) in exact:
                continue
            if any(DIGITS.fullmatch(item[0]) for item in window):
                continue
            left, right = window[0][1], window[-1][2]
            surface = segment[left:right]
            target = tuple(lemmas(surface))
            if not target:
                continue
            matches = [name for name, key in keys if key and set(target) < set(key)]
            if len(matches) == 1:
                found.append((surface, left, right))
    return found


def emit_fact(segment: str, text: str, start: int, name: str, left_token: int, right_token: int,
              dictionary, scoped_properties, facts: list, covered: list,
              require_measurement: bool = False) -> bool:
    """Создать факт для найденного имени (точного или с опечаткой). True, если создан.

    Значение берётся у того имени, рядом с которым оно стоит: сначала слева
    («650 Вт мощность»), и только если слева значения нет — справа. Чужое значение
    не отбирается: занятое предыдущим фактом измерение пропускается.

    `require_measurement` — для имени с опечаткой: пока написание не подтверждено
    пользователем, факт выдаётся только с однозначным измерением. Текстовое значение
    при неподтверждённом имени не выдумывается.
    """
    absolute = start + left_token
    if any(begin <= absolute < finish for begin, finish in covered):
        return False
    boundary = re.match(r"\s*(?:[:=]\s*)?", segment[right_token:])
    match_end = right_token + boundary.end()
    prefix = segment[max(0, left_token - 4):left_token]
    if re.search(r"без\s*$", prefix, re.I):
        if require_measurement:
            return False
        left, right = start + max(0, left_token - 4), start + match_end
        facts.append(make_fact(name, "нет", None, text[left:right], (left, right), len(facts), negated=True))
        covered.append((left, right))
        return True
    tail = segment[match_end:]
    measure = MEASUREMENT.match(tail)
    before = preceding_measurement(segment, left_token)
    if before is not None:
        value, unit, value_start = before
        left = start + value_start
        if not any(begin <= left < finish for begin, finish in covered):
            right = start + match_end
            facts.append(make_fact(name, value, unit, text[left:right], (left, right), len(facts)))
            covered.append((left, right))
            return True
    if measure:
        unit, consumed = measure["unit"], measure.end()
        current_props = [p for p in scoped_properties if p.name == name]
        if not unit and (not current_props or any(p.value_type == 'number' and
                p.numeric_semantics != 'designation' for p in current_props)):
            # «5 футов»: слово после числа — не единица справочника и не имя свойства.
            # Сохраняем написание, но не подтверждаем факт.
            unsupported = re.match(rf"\s+(?P<unit>{UNSUPPORTED_UNIT})", tail[consumed:])
            if unsupported and not is_property_name(unsupported["unit"], dictionary):
                unit, consumed = unsupported["unit"], consumed + unsupported.end()
        right = start + match_end + consumed
        facts.append(make_fact(name, measurement_value(measure["value"]), unit,
                               text[absolute:right], (absolute, right), len(facts)))
        covered.append((absolute, right))
        return True
    if require_measurement:
        return False
    value = tail.strip()
    # Хвост, который сам начинает новый факт («Мощность напряжение 220 В»), значением
    # не становится: иначе рождается мусорное значение, а настоящее имя теряется.
    if value and not starts_with_property_name(value, dictionary):
        lead = len(tail) - len(tail.lstrip())
        right = start + match_end + lead + len(value)
        facts.append(make_fact(name, value, None, text[absolute:right], (absolute, right), len(facts)))
        covered.append((absolute, right))
        return True
    return False


def note_correction(corrections: list | None, original: str, corrected: str, fact_id: str) -> None:
    """Записать в провенанс автоматическую замену имени.

    Замена не спрашивает разрешения, но и не молчит: в ответе API видно
    ««Мошность» → «Мощность»», а исходный текст остаётся в `raw` факта.
    """
    if corrections is None or canon(original) == canon(corrected):
        return
    corrections.append(Correction(fact_id=fact_id, kind="proposed_text_fix", original=original,
                                  normalized=corrected, confirmation_required=False,
                                  explanation="Опечатка имени исправлена автоматически: "
                                              f"«{original}» похоже на «{corrected}»"))


def dimension_candidates(properties, dimension: str) -> list:
    """Характеристики области, у которых есть допустимые значения в этой размерности.

    Нужны для случая «значение без имени»: «дрель 650 вт» — единица «Вт» называет
    размерность мощности, и если такая характеристика в области одна, значение
    принадлежит ей. Две характеристики одной размерности (например «минимальный» и
    «максимальный диаметр» оба в мм) — повод не угадывать: значение уходит
    неразобранным, а не приписывается наугад.
    """
    out = []
    for prop in properties:
        if getattr(prop, "value_type", None) != "number":
            continue
        for item in getattr(prop, "allowed", ()) or ():
            unit = get_unit(item.unit or item.raw_unit)
            if unit is not None and unit.dimension == dimension:
                out.append(prop)
                break
    return out


def bind_bare_measurements(segment: str, text: str, start: int, scoped_properties,
                           facts: list, covered: list) -> None:
    """Привязать измерения без имени к характеристике по размерности единицы.

    Реальный запрос «дрель 650 вт»: имени характеристики в тексте нет, но «Вт» —
    это мощность, и в области такая характеристика одна. Перевод в единицу
    характеристики делает C (0,65 кВт → 650 Вт), здесь сохраняется написание.
    """
    if not scoped_properties:
        return
    for match in MEASUREMENT.finditer(segment):
        if not match["unit"]:
            continue
        left, right = start + match.start(), start + match.end()
        if any(begin < right and left < finish for begin, finish in covered):
            continue
        unit = get_unit(match["unit"])
        if unit is None:
            continue
        candidates = dimension_candidates(scoped_properties, unit.dimension)
        if len(candidates) != 1:
            continue
        facts.append(make_fact(candidates[0].name, measurement_value(match["value"]),
                               match["unit"], text[left:right], (left, right), len(facts)))
        covered.append((left, right))


def measurement_options(fragment: str, properties) -> list:
    """Характеристики области, подходящие измерению по размерности единицы.

    Пустой список — измерение не с чем связать вовсе. Один вариант — значение
    привязывается само (`bind_bare_measurements`). Несколько — нужен вопрос
    пользователю: «что означает 12 мм: минимальный или максимальный диаметр?»
    """
    out: list = []
    seen: set[str] = set()
    for match in MEASUREMENT.finditer(fragment):
        if not match["unit"]:
            continue
        unit = get_unit(match["unit"])
        if unit is None:
            continue
        for prop in dimension_candidates(properties, unit.dimension):
            if prop.name not in seen:
                seen.add(prop.name)
                out.append(prop)
    return out


def measurement_option_names(fragment: str, properties) -> list[str]:
    """То же, но именами: так вопрос и ответ не зависят от объектов каталога."""
    return [prop.name for prop in measurement_options(fragment, properties)]



def extract(text: str, names: tuple[str, ...], *, scoped_properties=(), lexicon=None,
            corrections: list | None = None) -> tuple[list[Fact], list[Fragment]]:
    facts: list[Fact] = []
    covered: list[tuple[int, int]] = []
    dictionary = name_dictionary(names)
    fuzzy_index = fuzzy_name_index(names)
    lexicon = lexicon or {}
    prop_names = {p.name for p in scoped_properties}
    aliases = tuple((name, lemmas(alias)) for name, words in lexicon.get('name_aliases', {}).items()
                    if name in prop_names for alias in words)
    dictionary = tuple(sorted((*dictionary, *aliases), key=lambda entry: len(entry[1]), reverse=True))
    designation_facts(text, dictionary, scoped_properties, lexicon, facts, covered)
    # Absence is only normalized when the scope's own vocabulary represents it.
    # Otherwise retain a negative, uncertain fact; never invent a positive value.
    for name, allowed_absence in lexicon.get('absence', {}).items():
        props = [p for p in scoped_properties if p.name == name]
        if not props:
            continue
        for alias in lexicon.get('name_aliases', {}).get(name, []):
            wanted = lemmas(alias)
            tokens = token_spans(text)
            for i in range(len(tokens) - len(wanted) + 1):
                window = tokens[i:i + len(wanted)]
                if tuple(t[0] for t in window) != wanted:
                    continue
                left, right = window[0][1], window[-1][2]
                marker = re.search(r'\bбез\s*$', text[:left], re.I)
                if not marker:
                    continue
                if any(left < b and right > a for a, b in covered):
                    continue
                literal = next((v.value for p in props for v in p.allowed if canon(v.value or '') == canon(allowed_absence)), None)
                fact = make_fact(name, literal or text[left:right], None, text[marker.start():right],
                                 (marker.start(), right), len(facts), negated=True, unknown=not bool(literal))
                fact.surface_value = text[left:right]
                fact.extraction_evidence = [p.evidence for p in props] + [lexicon['evidence']]
                fact.negation_interpretation = 'canonical_absence' if literal else None
                if not literal:
                    fact.uncertainty_reason = 'Отсутствие не представлено допустимым значением выбранного свойства'
                facts.append(fact)
                covered.append(fact.span)
    for alias in lexicon.get('ambiguous_absence', []):
        for lemma, left, right in token_spans(text):
            if (lemma,) != lemmas(alias):
                continue
            marker = re.search(r'\bбез\s*$', text[:left], re.I)
            if marker and not any(left < b and right > a for a, b in covered):
                fact = make_fact('', text[left:right], None, text[marker.start():right],
                                 (marker.start(), right), len(facts), negated=True, unknown=True)
                fact.uncertainty_reason = 'Отрицание наличия части сохранено; положительное разрешённое значение не назначено'
                fact.extraction_evidence = [lexicon['evidence']]
                facts.append(fact)
                covered.append(fact.span)
    for start, end, segment in segments(text, dictionary):
        pair = re.match(r"\s*([^:=\n]{1,120})\s*[:=]\s*(.+?)\s*$", segment)
        if pair:
            if any(start < b and end > a for a, b in covered):
                continue
            # «650 Вт: мощность» — значение слева, имя справа. Разбор по двоеточию
            # иначе делает именем «650 Вт», а значением — название характеристики.
            leading = MEASUREMENT.fullmatch(pair[1].strip())
            if leading is not None and is_property_name(pair[2].strip(), dictionary):
                facts.append(make_fact(canonical_name(pair[2].strip(), dictionary),
                                       measurement_value(leading["value"]), leading["unit"],
                                       segment, (start, end), len(facts)))
                covered.append((start, end))
                continue
            value, unit, unconfirmed = split_measurement(pair[2], dictionary)
            # Имя из справочника, если форма слова подтверждена леммами. Если нет —
            # проверяем, не опечатка ли это: уверенная замена делается сразу,
            # неуверенная уходит в подсказку (её покажет поток коррекций).
            name = canonical_name(pair[1], dictionary)
            if name == pair[1]:
                corrected = confident_correction(pair[1], names, fuzzy_index)
                if corrected:
                    name = corrected
                    note_correction(corrections, pair[1], corrected, f"text:{start}:{end}:{len(facts)}")
            facts.append(make_fact(name, value, unit, segment,
                                   (start, end), len(facts), unknown=unconfirmed))
            covered.append((start, end))
            continue
        # Only explicit property phrases from vetted dictionaries, longest first.
        # Без имён свойств морфология не поднимается вовсе (ленивый синглтон).
        tokens = token_spans(segment) if dictionary else []
        hits: list[tuple[int, int, str, int, int]] = []
        for name, wanted in dictionary:
            if not wanted:
                continue
            for index in range(len(tokens) - len(wanted) + 1):
                window = tokens[index:index + len(wanted)]
                if tuple(item[0] for item in window) != wanted:
                    continue
                hits.append((start + window[0][1], -len(wanted), name, window[0][1], window[-1][2]))
        # Разбор идёт по тексту, а не по словарю: значение принадлежит тому имени,
        # рядом с которым оно стоит. Точные имена, имена с опечаткой и неполные имена
        # обрабатываются в ОДНОМ проходе по позиции: иначе «Мощность 650 Вт напряжение
        # 220 В» отдало бы 650 Вт точному «Напряжению» до очереди на «Мощность».
        combined: list[tuple[int, int, str, int, int, bool, str]] = [
            (absolute, length, name, left_token, right_token, False, "")
            for absolute, length, name, left_token, right_token in hits]
        # Уровень подсказки требует измерения (имя не подтверждено); уровень автозамены
        # ведёт себя как точное имя, но исходное написание уходит в провенанс.
        combined += [(start + left, -(right - left), name, left, right, not original, original or "")
                     for name, left, right, original
                     in typo_hits(segment, tokens, dictionary, fuzzy_index, names)]
        # Неполное имя характеристики: «минимальный диаметр» при «Минимальный диаметр
        # зажима патрона». Факт идёт с написанием пользователя и мягким (см.
        # `app/structured.py`): догадка не должна исключать коды.
        combined += [(start + left, -(right - left), name, left, right, True, "")
                     for name, left, right in partial_hits(segment, tokens, dictionary, names)]
        for _, _, name, left_token, right_token, with_typo, original in sorted(combined):
            if emit_fact(segment, text, start, name, left_token, right_token, dictionary,
                         scoped_properties, facts, covered, require_measurement=with_typo) and original:
                note_correction(corrections, original, name, facts[-1].id)
    # Reuse A's segmentation/provenance and the confirmed scope, never the global catalog.
    scope_parts = any(p.name in lexicon.get('parts', {}) for p in scoped_properties)
    values = sorted({value for _, value, _ in enum_dictionary(tuple(scoped_properties))} |
                    (set(lexicon.get('value_aliases', {})) if scope_parts else set()), key=len, reverse=True)
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
        # Morphological forms are scoped; reviewed noun/adjective aliases remain
        # separate from typo suggestions and preserve their source spelling.
        forms = [value, *(lexicon.get('value_aliases', {}).get(value, []) if scope_parts else ())]
        morph_tokens = token_spans(text)
        for form in forms:
            wanted = lemmas(form)
            for i in range(len(morph_tokens) - len(wanted) + 1):
                window = morph_tokens[i:i + len(wanted)]
                if wanted and tuple(t[0] for t in window) == wanted:
                    windows.append((window[0][1], window[-1][2]))
    for left, right in sorted(set(windows), key=lambda span: (span[0], -(span[1] - span[0]))):
        # Exact, fuzzy and morphological windows obey the same segmentation.
        # Reject cross-segment windows before assigning meaning or provenance.
        clause = next((c for c in clauses if c[0] <= left and right <= c[1]), None)
        if clause is None:
            continue
        if any(left < end and right > start for start, end in covered):
            continue
        value = text[left:right]
        exact, close = enum_targets(scoped_properties, value)
        if not exact:
            exact = [(p, target) for p, target, _ in enum_dictionary(tuple(scoped_properties))
                     if p.name in lexicon.get('parts', {}) and
                     any(lemmas(value) == lemmas(form) for form in lexicon.get('value_aliases', {}).get(target, []))]
        # An explicitly stated part can have an INVALID material. Extraction
        # reports it; membership is decided by C, not by vocabulary matching A.
        material = next((target for target, forms in lexicon.get('value_aliases', {}).items()
                         if any(lemmas(value) == lemmas(form) for form in (target, *forms))), None)
        stated_parts = [(p, material) for p in scoped_properties if material and
                        p.name in lexicon.get('parts', {}) and
                        explicit_part(text, left, right, lexicon['parts'][p.name])]
        if stated_parts:
            exact = stated_parts
        # A material names a part only if that part is stated. Without a part,
        # even a single allowed material owner is not enough linguistic evidence.
        part_candidates = [(p, v) for p, v in exact if p.name in lexicon.get('parts', {})]
        explicit_parts = []
        for p, target in part_candidates:
            if explicit_part(text, left, right, lexicon['parts'][p.name]):
                explicit_parts.append((p, target))
        if explicit_parts:
            exact = explicit_parts
        ownership = {(prop.semantic_key or prop.id, prop.subject) for prop, _ in exact}
        begin, end, _ = clause
        negation, uncertain = local_uncertainty(text, left, right, begin, end)
        raw_left, raw_right = (begin, end) if uncertain else (left, right)
        if len(ownership) == 1 and (not part_candidates or explicit_parts):
            prop = exact[0][0]
            canonical_value = value if canon(value) == canon(exact[0][1]) else exact[0][1]
            fact = make_fact(prop.name, canonical_value, None, text[raw_left:raw_right], (raw_left, raw_right), len(facts),
                             negated=negation, unknown=uncertain)
            fact.subject = prop.subject
        elif exact or close:
            fact = make_fact("", value, None, text[raw_left:raw_right], (raw_left, raw_right), len(facts),
                             negated=negation, unknown=uncertain)
        else:
            continue
        facts.append(fact)
        fact.surface_value = value
        fact.extraction_evidence = list(dict.fromkeys(p.evidence for p, _ in exact))
        if any(size_label(p.name, lexicon) for p, _ in exact) and foreign_size(text[begin:end]):
            fact.name, fact.unknown = '', True
            fact.assignment_options = [FactAssignment(name=p.name, value=target, subject=p.subject, evidence=p.evidence)
                                       for p, target in exact]
            fact.uncertainty_reason = 'Иностранная система размера не подтверждает значение российского размера'
        if exact and not fact.name and not fact.assignment_options:
            fact.assignment_options = [FactAssignment(name=p.name, value=target, subject=p.subject, evidence=p.evidence)
                                       for p, target in exact]
            fact.uncertainty_reason = ('Не указана часть товара, к которой относится материал' if part_candidates
                                       else 'Значение подходит к нескольким разным свойствам выбранной области')
        covered.append((left, right))
    # Numeric designation may precede its explicit label ("40 размер").
    for prop in scoped_properties:
        if size_label(prop.name, lexicon):
            continue
        if prop.numeric_semantics != 'designation' or not prop.numeric_semantics_evidence:
            continue
        labels = [prop.name, *lexicon.get('name_aliases', {}).get(prop.name, [])]
        for label in labels:
            for match in re.finditer(rf'(?<![\w.,])({NUMBER})\s+({re.escape(label)})(?!\w)', text, re.I):
                if any(match.start() < b and match.end() > a for a, b in covered):
                    continue
                fact = make_fact(prop.name, match[1], None, match[0], match.span(), len(facts))
                fact.extraction_evidence = [prop.numeric_semantics_evidence]
                facts.append(fact)
                covered.append(match.span())
    # Preserve anonymous physical measures as facts without guessing their role.
    for match in MEASUREMENT.finditer(text):
        if not match['unit'] or any(match.start() < b and match.end() > a for a, b in covered):
            continue
        fact = make_fact('', measurement_value(match['value']), match['unit'], match[0], match.span(), len(facts))
        fact.uncertainty_reason = 'Не установлено, какое свойство измерено'
        from app.units import get_unit
        unit = get_unit(fact.unit)
        for prop in scoped_properties:
            if prop.value_type != 'number' or not prop.verified:
                continue
            if unit and any(get_unit(item.unit) and get_unit(item.unit).dimension == unit.dimension for item in prop.allowed):
                fact.assignment_options.append(FactAssignment(name=prop.name, value=fact.value, unit=fact.unit,
                                                              subject=prop.subject, evidence=prop.evidence))
        # Единица измерения называет характеристику, если такая в области ОДНА:
        # «дрель 650 вт» — это мощность, и значение принадлежит ей сразу, без вопроса.
        # Если подходящих несколько («минимальный» и «максимальный диаметр» оба в мм),
        # значение остаётся без имени, и вопрос задаёт поток коррекций.
        # В контексте размеров (size systems) не угадываем вовсе: там значение зависит
        # от системы (EU/FR/RU), и решение остаётся за пользователем — как и требует
        # независимый набор проверок размеров.
        size_context = any(size_label(prop.name, lexicon) for prop in scoped_properties)
        if len(fact.assignment_options) == 1 and not size_context:
            option = fact.assignment_options[0]
            fact.name, fact.subject = option.name, option.subject
            fact.extraction_evidence = list(dict.fromkeys([*fact.extraction_evidence, option.evidence]))
            fact.uncertainty_reason = None
            fact.assignment_options.clear()
        facts.append(fact)
        covered.append(match.span())
    canonicalize_facts(facts, scoped_properties, lexicon)
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
        import json
        result.append(Fact(id=item.id, name=item.name, value=item.value, values=item.values, unit=item.unit,
                           raw=json.dumps(item.values, ensure_ascii=False) if item.values is not None else item.value,
                           source="input", unknown=item.unknown or (item.values is None and not item.value.strip()), subject=item.subject))
    return result
