"""Pure validation, package rounding and presentation of grocery purchases."""

import re
from decimal import ROUND_CEILING, Decimal

from fitagent.vkusvill import MCP_ERROR, trusted_link

UNIT_SCALES = {"г": ("г", 1), "кг": ("г", 1000), "мл": ("мл", 1), "л": ("мл", 1000)}


def unit_scale(unit, measure):
    if unit not in UNIT_SCALES or UNIT_SCALES[unit][0] != measure:
        raise ValueError(MCP_ERROR)
    return UNIT_SCALES[unit][1]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(MCP_ERROR)
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(MCP_ERROR)
    return result


def product_data(item):
    product_id = item["id"]
    if type(product_id) is not int or not 1 <= product_id <= 999999999:
        raise ValueError(MCP_ERROR)
    name = item["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 250:
        raise ValueError(MCP_ERROR)
    price = number(item["price"]["current"])
    if item["price"]["currency"] != "RUB":
        raise ValueError(MCP_ERROR)
    properties = item["properties"]
    if not isinstance(properties, list) or len(properties) > 50:
        raise ValueError(MCP_ERROR)
    for prop in properties:
        if not isinstance(prop, dict) or not all(
            isinstance(prop.get(key), str) for key in ("name", "value")
        ):
            raise ValueError(MCP_ERROR)
        if len(prop["value"]) > 5000:
            raise ValueError(MCP_ERROR)
    if not any("состав" in prop["name"].lower() and prop["value"].strip() for prop in properties):
        raise ValueError(MCP_ERROR)
    if not any("ценност" in prop["name"].lower() and prop["value"].strip() for prop in properties):
        raise ValueError(MCP_ERROR)
    if not isinstance(item["unit"], str):
        raise ValueError(MCP_ERROR)
    unit = item["unit"].strip().rstrip(".").lower()
    if unit != "шт" and unit not in UNIT_SCALES:
        raise ValueError(MCP_ERROR)
    return {
        "id": product_id,
        "name": name,
        "price": float(price),
        "unit": item["unit"],
        "weight": item.get("weight"),
        "url": trusted_link(item["url"]),
        "properties": properties,
    }


def purchase(product, amount, measure):
    amount = number(amount)
    unit = product["unit"].lower().strip().rstrip(".")
    if unit == "шт":
        if measure == "шт":
            quantity = amount.to_integral_value(rounding=ROUND_CEILING)
        else:
            weight = product["weight"]
            size = number(weight["value"])
            size *= unit_scale(weight["unit"].lower(), measure)
            quantity = (amount / size).to_integral_value(rounding=ROUND_CEILING)
    else:
        quantity = (amount / unit_scale(unit, measure)).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
    if not Decimal("0.01") <= quantity <= 40:
        raise ValueError(MCP_ERROR)
    total = (number(product["price"]) * quantity).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return dict(
        product, quantity=float(quantity), total=total, amount=float(amount), measure=measure
    )


def plain(value):
    # Render external names as text rather than executable Markdown/terminal escapes.
    value = re.sub(r"<[^>]*>", "", value)
    value = " ".join("".join(c for c in value if c.isprintable()).split())
    return re.sub(r"([\\`*_{}\[\]()<>!|#])", r"\\\1", value)


def money(value):
    return f"{value:.2f}".replace(".", ",") + " ₽"


def render_purchases(items, *, missing=(), budget=None):
    lines = ["### Продукты ВкусВилла"]
    if items:
        lines.extend(["На одного человека. Покупаемые упаковки могут оставить запас.", ""])
    for item in items:
        measure = "товарных упаковок" if item["measure"] == "шт" else item["measure"]
        lines.append(
            f"- {plain(item['name'])} — {item['quantity']:g} {plain(item['unit'])} × {money(item['price'])} = **{money(item['total'])}**; для рациона: {item['amount']:g} {measure}."
        )
    if items:
        label = "Сумма по найденным позициям" if missing else "Итого за закупку"
        total = money(sum(item["total"] for item in items))
        lines.extend(
            [
                "",
                f"**{label}: {total}.** Доставка не включена.",
                "КБЖУ ориентировочные. Цены и наличие могут измениться; проверяйте их во ВкусВилле.",
            ]
        )
    if missing:
        lines.extend(
            ["", "**Не удалось подобрать или рассчитать:** " + ", ".join(map(plain, missing)) + "."]
        )
    if budget is not None:
        lines.extend(
            [
                "",
                f"**Закупка превышает бюджет {money(budget)}.** Попросите подобрать более дешёвый вариант.",
            ]
        )
    if missing or budget is not None:
        lines.extend(["", "Ссылка на корзину не создана."])
    return "\n".join(lines)
