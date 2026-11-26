"""Provider-reported token accounting; no local tokenizer or estimates."""


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def api_usage(data):
    data = data if isinstance(data, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": token_count(usage.get("prompt_tokens", usage.get("input_tokens"))),
        "output_tokens": token_count(usage.get("completion_tokens", usage.get("output_tokens"))),
        "cached_tokens": token_count(
            details.get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
        ),
    }


def iter_usage(events):
    """Match concurrent calls by ID and retain serial pairing for older audit records."""
    calls = []
    pending = {}
    legacy = None
    for kind, details in events:
        request_id = details.get("request_id")
        purpose = details.get("purpose")
        if kind == "api_request":
            entry = {"purpose": purpose}
            calls.append(entry)
            if isinstance(request_id, str) and request_id:
                pending[request_id] = entry
            else:
                legacy = entry
        elif kind == "api_usage":
            if isinstance(request_id, str) and request_id:
                entry = pending.pop(request_id, None)
            else:
                entry = legacy if legacy is not None and legacy["purpose"] == purpose else None
                legacy = None
            if entry is None:
                calls.append(details)
            else:
                entry.update(details)
    yield from calls


def collect_usage(events, include_history=False):
    """Aggregate known counts, optionally retaining the cumulative value per call."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    known = dict.fromkeys(totals, 0)
    report = {"requests": 0, "incomplete_requests": 0, "last_answer": None}
    if include_history:
        report["history"] = []
    for request in iter_usage(events):
        report["requests"] += 1
        counts = {key: token_count(request.get(key)) for key in totals}
        for key, value in counts.items():
            if value is not None:
                totals[key] += value
                known[key] += 1
        incomplete = None in counts.values()
        if incomplete:
            report["incomplete_requests"] += 1
        if request.get("purpose") in ("answer", "nutrition_plan"):
            report["last_answer"] = counts
        if include_history:
            report["history"].append(
                {
                    "request": report["requests"],
                    "total_tokens": sum(totals.values()) if any(known.values()) else None,
                    "incomplete": incomplete,
                }
            )
    report.update(
        {
            key: value if known[key] or not report["requests"] else None
            for key, value in totals.items()
        }
    )
    report["total_tokens"] = (
        sum(totals.values()) if any(known.values()) or not report["requests"] else None
    )
    return report


def usage_text(report):
    def number(value):
        return "нет данных" if value is None else f"{value:,}".replace(",", " ")

    last = report["last_answer"]
    current = (
        f"Токены · контекст: {number(last['input_tokens'])} · ответ: {number(last['output_tokens'])}"
        if last is not None
        else "Токены · запросов в чате ещё нет."
    )
    total = (
        f"За переписку · вход: {number(report['input_tokens'])} · "
        f"выход: {number(report['output_tokens'])} · всего: {number(report['total_tokens'])}"
    )
    completeness = f"Обращений к API: {report['requests']}"
    if report["incomplete_requests"]:
        completeness += (
            f" · без полной статистики: {report['incomplete_requests']}. "
            "Сумма только по известным значениям."
        )
    return "\n".join((current, total, completeness))
