"""Explicit resolution of contextual mapping suggestions; never infer a user's fact."""
import hashlib

from app.mapping import map_facts
from app.schemas import Correction, Issue, Question, QuestionOption
from app.extraction import enum_targets


def resolve_suggestions(category_id, positions, facts, answers):
    proposals = {}
    for position in positions:
        _, _, corrections = map_facts(position, facts)
        for correction in corrections:
            proposals.setdefault(correction.fact_id, set()).add(correction.normalized)
    current_answers = {answer.question_id: answer for answer in answers}
    pending, rejected, accepted = [], set(), []
    for fact in facts:
        targets = sorted(proposals.get(fact.id, ()))
        if not targets:
            continue
        identity = repr((category_id, fact.id, fact.name, fact.value, fact.unit, fact.raw, targets))
        qid = "mapping:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        answer = current_answers.get(qid)
        if answer and not answer.unknown and answer.value == "reject":
            rejected.add(fact.id)
        elif answer and not answer.unknown and answer.value in {"accept:" + name for name in targets}:
            accepted.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=fact.name,
                normalized=answer.value.removeprefix("accept:"), explanation="Сопоставление явно подтверждено пользователем"))
            fact.name = answer.value.removeprefix("accept:")
        else:
            pending.append((fact, Question(id=qid, kind="correction",
                title=f"«{fact.name}: {fact.value} {fact.unit or ''}» — уточните характеристику",
                reason="Похожее имя может изменить проверку товара; сопоставление требует вашего решения",
                property_name=fact.name,
                options=[QuestionOption(value="accept:" + name, label=f"Это «{name}»") for name in targets] +
                        [QuestionOption(value="reject", label="Это дополнительные сведения")]),
                Issue(code="unresolved_property_correction", fact_id=fact.id,
                      explanation=f"Не подтверждено сопоставление «{fact.name}» с «{' / '.join(targets)}»",
                      confirmation_required=True, suggested_action="Подтвердите или отклоните предлагаемое сопоставление")))
    properties = tuple(prop for position in positions for prop in position.properties)
    for fact in facts:
        if fact.unknown and fact.name:
            continue
        exact, close = enum_targets(properties, fact.value, fact.name, fact.subject if fact.name else None)
        # An explicitly named exact value is already handled by contextual mapping/C.
        if fact.name and exact:
            continue
        targets = sorted({(prop.name, value, prop.subject) for prop, value in (exact or close)})
        if not targets:
            continue
        identity = repr((category_id, fact.id, fact.name, fact.value, fact.raw, targets))
        qid = "value:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        answer = current_answers.get(qid)
        # Negated/uncertain fragments may be preserved or explicitly edited, never affirmed by typo correction.
        choices = {} if fact.negated or fact.unknown else {f"accept_value:{i}": target for i, target in enumerate(targets)}
        if answer and not answer.unknown and answer.value == "reject":
            rejected.add(fact.id)
            # Rejected assignment remains source information, not evidence for C.
            fact.name = ""
        elif answer and not answer.unknown and answer.value in choices:
            name, value, subject = choices[answer.value]
            accepted.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=fact.value,
                normalized=value, explanation=f"Пользователь подтвердил «{name}: {value}»; все факты проверены заново"))
            fact.name, fact.value, fact.subject = name, value, subject
        else:
            original = fact.value
            pending.append((fact, Question(id=qid, kind="correction",
                title=f"Уточните значение «{original}»",
                reason=("Отрицание или неопределённость нельзя заменить положительным значением; исправьте факт явно или сохраните дополнительно"
                        if fact.negated or fact.unknown else
                        "Допустимое значение или его назначение требует вашего подтверждения в выбранной области"),
                property_name=fact.name or None,
                options=[QuestionOption(value=token, label=f"{name}: {value}")
                         for token, (name, value, subject) in choices.items()] +
                        [QuestionOption(value="reject", label="Сохранить как дополнительные сведения")]),
                Issue(code="unresolved_value_correction", fact_id=fact.id,
                    explanation=f"Не подтверждено исправление или назначение «{original}»",
                    confirmation_required=True, suggested_action="Подтвердите или отклоните предложенное значение")))
            accepted.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=original,
                normalized=" / ".join(f"{name}: {value}" for name, value, _ in targets),
                explanation="Варианты допустимых значений выбранной области; исправление не применено",
                confirmation_required=True))
    # Suggestion discovery is not evidence. Recompute mappings after any decision.
    for fact in facts:
        fact.mappings.clear()
    return pending, rejected, accepted
