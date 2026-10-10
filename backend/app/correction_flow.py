"""Explicit resolution of contextual mapping suggestions; never infer a user's fact."""
import hashlib

from app.mapping import map_facts
from app.schemas import Correction, Issue, Question, QuestionOption


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
    return pending, rejected, accepted
