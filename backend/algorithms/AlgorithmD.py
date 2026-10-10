"""Алгоритм D — выбор следующего уточняющего вопроса.

Вызывается только тогда, когда C не смог оставить один код. Вход: пул кандидатов
после C и список уже заданных свойств. Выход: один вопрос либо причина остановки.

Порядок решения:
  1) конфликт факта, блокировка данных или дефект источника -> вопрос не задаём,
     возвращаем причину (это работа диагностики E);
  2) тип товара не подтверждён -> подтверждаем тип, а не значение;
  3) одинаковые ограничения у всех кандидатов -> по значениям спрашивать нельзя,
     нужен смысловой признак либо статус ambiguous;
  4) иначе -> неизвестное свойство, которое делит кандидатов;
  5) разделяющих свойств нет -> добираем обязательные поля карточки.

Запуск: next_question(result.survivor_positions(index), answered_from_facts(facts), index)
"""
from __future__ import annotations

import collections
from typing import Optional

from .contract import Fact, Question, Unit
from .ktru_index import Characteristic, KtruIndex, normalize_name

MAX_ALTERNATIVES = 3


# ------------------------------------------------------------- вспомогательное
def answered_from_facts(facts: list) -> set:
    """Определения, которые уже закрыты фактами: код, идентификатор и имя.

    Учитываются и `definition_alternatives`, иначе один и тот же вопрос может
    вернуться второй раз под другим определением.
    """
    answered = set()
    for f in facts:
        if not f.is_hard:
            continue
        if f.def_id:
            answered.add(f.def_id)
        if f.name:
            answered.add(f.name)
            answered.add(normalize_name(f.name))
    return answered


def _closed(answered: set, name: str, def_id: Optional[str]) -> bool:
    if def_id and def_id in answered:
        return True
    if name in answered:
        return True
    key = normalize_name(name)
    return key in answered or any(key == normalize_name(a) for a in answered if isinstance(a, str))


def _bucket_key(char: Characteristic) -> frozenset:
    return frozenset(a.key() for a in char.alternatives)


def _answer_kind(char: Characteristic) -> str:
    if char.is_quantitative:
        if all(a.kind in ("number", "range") for a in char.alternatives):
            return "number_set" if str(char.kind) == "3" else "number"
        return "choice"
    labels = {str(a.text).strip().lower() for a in char.alternatives if a.kind == "text"}
    if labels <= {"да", "нет"}:
        return "boolean"
    return "text_set" if str(char.kind) == "3" else "choice"


def _choices(char: Characteristic, chars=None) -> Optional[list]:
    """Варианты ответа с числом кандидатов, которые каждый вариант оставляет."""
    kind = _answer_kind(char)
    if kind not in ("choice", "boolean"):
        return None
    seen, out = set(), []
    for a in [a for c in (chars or [char]) for a in c.alternatives]:
        label = a.text if a.kind == "text" else a.describe()
        key = (label or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append({"value": label, "candidates": None})
    return out


def _unit_of(char: Characteristic) -> Optional[Unit]:
    alt = next((a for a in char.alternatives if a.unit_code), None)
    if alt is None:
        return None
    return Unit(code=alt.unit_code, text=alt.unit_text)


def _bounds(char: Characteristic) -> Optional[dict]:
    los = [a.lo for a in char.alternatives if a.kind == "range" and a.lo is not None]
    his = [a.hi for a in char.alternatives if a.kind == "range" and a.hi is not None]
    if not los and not his:
        return None
    return {"min": min(los) if los else None, "max": max(his) if his else None}


# ------------------------------------------------------------------- основное
def next_question(pool: list, answered: set, index: KtruIndex, *,
                  type_verified: bool = False, blocking_issues: Optional[list] = None,
                  fill_required: bool = True) -> Question:
    """Возвращает следующий вопрос или Question со stop_reason."""
    blocking = list(blocking_issues or [])

    if blocking:
        return Question(reason="fixes_conflict", answer_kind="text", candidates_before=len(pool),
                        stop_reason="blocking_issue:" + ",".join(sorted(set(blocking))))

    if not pool:
        return Question(reason="splits_candidates", answer_kind="text", stop_reason="no_candidates")

    if not type_verified:
        return Question(
            reason="confirms_type", answer_kind="choice", candidates_before=len(pool),
            choices=[{"value": p.name, "code": p.code, "candidates": None} for p in pool[:5]],
        )

    if len(pool) > 1 and len({p.structure_hash() for p in pool}) == 1:
        return Question(
            reason="meaningful_difference", answer_kind="text", candidates_before=len(pool),
            buckets=1, worst_case=len(pool), stop_reason="value_indistinguishable",
        )

    names, seen = [], set()
    for p in pool:
        for c in p.characteristics:
            key = normalize_name(c.name)
            if key not in seen:
                seen.add(key)
                names.append(c.name)

    splitting = []
    for name in names:
        applicable, chars = [], []
        for p in pool:
            c = p.char_by_name(name)
            if c is None or not c.alternatives:
                continue                               # у позиции такого свойства нет
            applicable.append(p)
            chars.append(c)
        if len(applicable) < 2:
            continue
        if _closed(answered, name, chars[0].code):
            continue                                   # уже спрашивали
        buckets = collections.defaultdict(list)
        for c, p in zip(chars, applicable):
            buckets[_bucket_key(c)].append(p.code)
        if len(buckets) == 1:
            continue                                   # ответ не различает тех, у кого свойство есть
        sizes = sorted((len(v) for v in buckets.values()), reverse=True)
        required = any(c.is_required for c in chars)
        # оценка: сколько кандидатов останется в худшем случае (включая тех,
        # у кого свойства нет — их ответ не исключает), затем более простое для ответа свойство
        simplicity = 0 if len(buckets) <= 8 else 1
        worst = sizes[0] + (len(pool) - len(applicable))
        splitting.append(((worst, simplicity, -len(buckets), not required), name, chars[0],
                          len(buckets), sizes[0], len(applicable)))

    if splitting:
        splitting.sort(key=lambda x: x[0])
        _, name, char, buckets, worst_bucket, applicable = splitting[0]
        alternatives = [
            {"property": {"def_id": c.code, "name": n},
             "expected_effect": {"buckets": b, "worst_case": w, "applicable": a}}
            for _, n, c, b, w, a in splitting[1:1 + MAX_ALTERNATIVES]
        ]
        here = [p.char_by_name(name) for p in pool if p.char_by_name(name)]
        units = {(a.unit_code, a.unit_text) for c in here for a in c.alternatives}
        return Question(
            def_id=char.code, name=name, reason="splits_candidates", answer_kind=_answer_kind(char),
            unit=_unit_of(char) if len(units) == 1 else None, choices=_choices(char, here), bounds=None,
            candidates_before=len(pool), buckets=buckets, worst_case=worst_bucket + len(pool) - applicable, alternatives=alternatives,
        )

    if fill_required:
        for name in names:
            chars = [p.char_by_name(name) for p in pool]
            here = [c for c in chars if c is not None and c.alternatives]
            if not here:
                continue
            if _closed(answered, name, here[0].code):
                continue
            if any(c.is_required for c in here):
                char = here[0]
                return Question(
                    def_id=char.code, name=name, reason="fills_required", answer_kind=_answer_kind(char),
                    unit=_unit_of(char), choices=_choices(char, here), bounds=_bounds(char) if len(here) == 1 else None,
                    candidates_before=len(pool),
                    buckets=len({_bucket_key(c) for c in here}), worst_case=len(pool),
                )

    return Question(reason="splits_candidates", answer_kind="text", candidates_before=len(pool),
                    stop_reason="already_unique" if len(pool) == 1 else "no_splitting_property")
