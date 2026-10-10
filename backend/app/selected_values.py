"""One representation shared by backend checks and the C adapter."""
from app.search import canon


def selected_values(prop, fact):
    if fact.values is not None:
        return fact.values
    # Preserve an exact compound literal. Legacy comma lists are accepted only
    # for textual kind=3 dictionaries without comma-containing source literals.
    allowed = [item.value for item in prop.allowed if item.kind == "enum" and item.value]
    if prop.kind == "3" and prop.value_type == "text" and "," in fact.value:
        if canon(fact.value) not in {canon(v) for v in allowed} and not any("," in v for v in allowed):
            return [part.strip() for part in fact.value.split(",") if part.strip()]
    return [fact.value]
