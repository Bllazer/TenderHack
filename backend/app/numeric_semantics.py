"""Reviewed interpretations, separate from generic constraint algorithms.

No OKEI is invented. These characteristic identifiers were inspected in actual
XML; the source names count physical parts, rather than expressing a magnitude
or a numeric class. Unknown identifiers retain unknown semantics.
"""
ADAPTER_VERSION = "clean5-adapter-v2-kind3-applicability"
REGISTRY_VERSION = "numeric-semantics-v1"
COUNT_RULES = {
    "372218d3-6": ("Количество бортов", "backend/docs/reviews/stage2/xml/25.99.29.120-00000003.xml#372218d3-6"),
    "dcc501a9-e": ("Количество ребер жесткости", "backend/docs/reviews/stage2/xml/25.99.29.120-00000003.xml#dcc501a9-e"),
}


def interpretation(row):
    rule = COUNT_RULES.get(row.get("code"))
    if row.get("type") == "2" and rule and row.get("name") == rule[0]:
        return "count", f"{REGISTRY_VERSION}:{rule[1]}"
    return "unknown", None
