"""Explicit resolution of contextual mapping suggestions; never infer a user's fact."""
import hashlib

from app.mapping import map_facts
from app.schemas import Correction, Issue, Question, QuestionOption
from app.extraction import enum_targets


def resolve_suggestions(category_id, positions, facts, answers, *, extra_properties=()):
    proposals = {}
    for position in positions:
        _, _, corrections = map_facts(position, facts)
        for correction in corrections:
            if any(f.id == correction.fact_id and f.card_mappings for f in facts):
                continue
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
    properties = (*tuple(prop for position in positions for prop in position.properties), *extra_properties)
    for fact in facts:
        relevant_rule = any(p.name == fact.name and p.subject == fact.subject
                            for position in positions for p in position.properties)
        anonymous_measure = not fact.name and fact.unit and not fact.assignment_options and fact.uncertainty_reason
        if anonymous_measure or (fact.unknown and (fact.assignment_options or (fact.source == 'text' and relevant_rule))):
            identity = repr((category_id, fact.id, fact.raw, fact.name, fact.value, fact.assignment_options))
            qid = 'uncertainty:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
            answer = current_answers.get(qid)
            if answer and not answer.unknown and answer.value == 'reject':
                rejected.add(fact.id)
                fact.name = ''
                fact.card_mappings.clear()
                fact.assignment_options.clear()
                continue
            reason = fact.uncertainty_reason or 'Отрицание или неопределённость не подтверждает положительное значение'
            peers = [f.raw for f in facts if f.id != fact.id and fact.name and f.name == fact.name]
            if peers:
                reason += '; сопоставленные фрагменты: ' + ' / '.join([*peers, fact.raw])
            issue = Issue(code='unresolved_fact_uncertainty', fact_id=fact.id,
                explanation=f'«{fact.raw}»: {reason}', confirmation_required=True,
                suggested_action='Исправьте факт через attributes.replaces либо сохраните исходные сведения дополнительно')
            # "Don't know" leaves the blocker visible, but does not ask the same
            # question forever. Explicit card edits / a new answer can resolve it.
            question = None if answer and answer.unknown else Question(id=qid, kind='correction',
                title=f'Уточните противоречащие или неопределённые сведения: «{fact.raw.strip()}»', reason=reason,
                property_name=fact.name or None,
                options=[QuestionOption(value='reject', label='Сохранить как дополнительные сведения')])
            pending.append((fact, question, issue))
            continue
        if fact.unknown and fact.name:
            continue
        exact, close = enum_targets(properties, fact.value, fact.name, fact.subject if fact.name else None)
        # An explicitly named exact value is already handled by contextual mapping/C.
        if fact.name and exact and not fact.assignment_options:
            continue
        targets = sorted({(option.name, option.value, option.subject, option.unit or fact.unit)
                          for option in fact.assignment_options} if fact.assignment_options else
                         {(prop.name, value, prop.subject, fact.unit) for prop, value in (exact or close)}, key=repr)
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
            fact.card_mappings.clear()
            fact.assignment_options.clear()
        elif answer and not answer.unknown and answer.value in choices:
            name, value, subject, unit = choices[answer.value]
            accepted.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=fact.value,
                normalized=value, explanation=f"Пользователь подтвердил «{name}: {value}»; все факты проверены заново"))
            fact.name, fact.value, fact.subject, fact.unit = name, value, subject, unit
            fact.uncertainty_reason = None
            fact.assignment_options.clear()
            fact.card_mappings = [p.id for p in extra_properties if p.name == name and p.subject == subject]
        else:
            original = fact.value
            display = f'{original} {fact.unit or ""}'.strip()
            question = None if answer and answer.unknown and fact.assignment_options else Question(id=qid, kind="correction",
                title=(f"К какому свойству относится «{display}»?" if fact.assignment_options else
                       f"Уточните значение «{original}»"),
                reason=("Отрицание или неопределённость нельзя заменить положительным значением; исправьте факт явно или сохраните дополнительно"
                        if fact.negated or fact.unknown else
                        fact.uncertainty_reason or "Допустимое значение или его назначение требует вашего подтверждения в выбранной области"),
                property_name=fact.name or None,
                options=[QuestionOption(value=token, label=f"{name}: {value} {unit or ''}".strip())
                         for token, (name, value, subject, unit) in choices.items()] +
                        [QuestionOption(value="reject", label="Сохранить как дополнительные сведения")])
            pending.append((fact, question,
                Issue(code="unresolved_value_correction", fact_id=fact.id,
                    explanation=f"Не подтверждено исправление или назначение «{original}»",
                    confirmation_required=True, suggested_action="Подтвердите или отклоните предложенное значение")))
            accepted.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=original,
                normalized=" / ".join(f"{name}: {value} {unit or ''}".strip() for name, value, _, unit in targets),
                explanation="Варианты допустимых значений выбранной области; исправление не применено",
                confirmation_required=True))
    # Suggestion discovery is not evidence. Recompute mappings after any decision.
    for fact in facts:
        fact.mappings.clear()
    return pending, rejected, accepted
