"""Layer classification is independent of applicability, provenance and version lifetimes."""

from fitagent.memory_schema import SAFETY_CATEGORIES, reported_order

PREFERENCES = {"food_preferences", "communication"}
LONG_TERM = {"equipment", "training_experience"}
SHORT_TERM = {"symptoms", "recovery"}
LAYER_LABELS = {
    "safety": "Ограничения безопасности",
    "preferences": "Предпочтения",
    "long": "Долгосрочная память",
    "medium": "Среднесрочная память",
    "short": "Краткосрочная память",
}


def memory_layer(category):
    if category in SAFETY_CATEGORIES:
        return "safety"
    if category in PREFERENCES:
        return "preferences"
    if category in LONG_TERM:
        return "long"
    if category in SHORT_TERM:
        return "short"
    return "medium"


def inventory_facts(facts):
    """Measurements form a time series, not an ever-growing always-on prompt."""
    normal = [fact for fact in facts if fact["category"] != "measurements"]
    measured = sorted(
        (fact for fact in facts if fact["category"] == "measurements"),
        key=reported_order,
    )
    return normal + measured[-30:]
