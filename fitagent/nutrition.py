"""Nutrition routing and bounded anonymous shopping, independent of the terminal."""

import json
import re

from fitagent.memory_schema import unique_object, validate_shape
from fitagent.nutrition_schema import (
    QUERY_PROMPT,
    QUERY_SCHEMA,
    ROUTE_PROMPT,
    ROUTE_SCHEMA,
    SELECTION_PROMPT,
    SELECTION_SCHEMA,
)
from fitagent.shopping import number, product_data, purchase, render_purchases
from fitagent.vkusvill import MCP_ERROR

NUTRITION_ERROR = "Не удалось уточнить условия подбора питания. Повторите запрос, указав период и формат еды: готовая еда или готовка."


class NutritionShopping:
    def __init__(self, client, catalog, audit=None):
        self.client, self.catalog = client, catalog
        self.audit = audit or (lambda kind, details: None)

    def call_catalog(self, operation, *args, **kwargs):
        self.audit("mcp_started", {"operation": operation})
        try:
            result = getattr(self.catalog, operation)(*args, **kwargs)
            if operation == "search" and not isinstance(result, list):
                raise ValueError(MCP_ERROR)
        except Exception:
            self.audit("mcp_failed", {"operation": operation})
            raise
        count = len(result) if operation == "search" else len(args[0])
        self.audit("mcp_finished", {"operation": operation, "count": count})
        return result

    def skip_cart(self, reason):
        self.audit("nutrition_cart_skipped", {"reason": reason})
        return None

    def price_report(self, purchases, missing, budget):
        total = sum(item["total"] for item in purchases)
        over_budget = budget is not None and total > number(budget)
        reason = "incomplete_selection" if missing else "budget" if over_budget else None
        if reason:
            self.skip_cart(reason)
        return {
            "text": render_purchases(
                purchases, missing=missing, budget=budget if over_budget else None
            ),
            "products": []
            if reason
            else [{"id": item["id"], "q": item["quantity"]} for item in purchases],
            "evidence": json.dumps(purchases, ensure_ascii=False, default=str),
        }

    def structured(self, prompt, schema, payload, purpose):
        raw = self.client.generate_structured(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            schema,
            purpose=purpose,
        )
        value = json.loads(raw, object_pairs_hook=unique_object)
        validate_shape(value, schema)
        return value

    def route(self, payload):
        try:
            value = self.structured(ROUTE_PROMPT, ROUTE_SCHEMA, payload, "nutrition_route")
            if value["action"] == "none":
                valid = (
                    not value["question"]
                    and not value["period"]
                    and value["food_format"] == "unknown"
                    and not value["cheaper"]
                    and not value["goal_related"]
                )
            elif value["action"] == "clarify":
                valid = bool(value["question"].strip()) and not value["goal_related"]
            else:
                valid = (
                    not value["question"]
                    and value["period"].strip()
                    and value["food_format"] != "unknown"
                )
            if not valid:
                raise ValueError
            return value
        except Exception:
            raise ValueError(NUTRITION_ERROR) from None

    def prepare(self, payload, diet):
        """Return priced matches even if a basket cannot be created."""
        failure = "selection_error"
        try:
            queries = self.structured(
                QUERY_PROMPT,
                QUERY_SCHEMA,
                {**payload, "recommended_diet": diet},
                "nutrition_queries",
            )["queries"]
            if len(set(queries)) != len(queries) or any(
                not re.fullmatch(r"[а-яёА-ЯЁ][а-яёА-ЯЁ -]{0,79}", query) for query in queries
            ):
                return self.skip_cart("invalid_queries")
            candidates = []
            for index, query in enumerate(queries):
                failure = "mcp_error"
                items = self.call_catalog(
                    "search", query, cheaper=payload["nutrition_request"]["cheaper"]
                )
                failure = "product_data"
                if len(items) > 5:
                    return self.skip_cart("product_data")
                products = []
                for item in items:
                    try:
                        products.append(product_data(item))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                candidates.append({"query_index": index, "query": query, "candidates": products})
            searchable = [group for group in candidates if group["candidates"]]
            if not searchable:
                return self.price_report([], queries, None)
            failure = "selection_error"
            chosen = self.structured(
                SELECTION_PROMPT,
                SELECTION_SCHEMA,
                {**payload, "recommended_diet": diet, "catalog": searchable},
                "nutrition_selection",
            )["items"]
            indices = {item["query_index"] for item in chosen}
            if len(indices) != len(chosen) or not indices <= {
                group["query_index"] for group in searchable
            }:
                return self.skip_cart("selection_error")
            failure = "quantity_error"
            grouped = {}
            for selection in chosen:
                amount = number(selection["amount"])
                source = candidates[selection["query_index"]]["candidates"]
                product = next(item for item in source if item["id"] == selection["id"])
                entry = grouped.setdefault(
                    product["id"],
                    {
                        "product": product,
                        "amount": 0,
                        "measure": selection["measure"],
                        "indices": [],
                    },
                )
                if entry["measure"] != selection["measure"] or entry["product"] != product:
                    return self.skip_cart("product_conflict")
                entry["amount"] += amount
                entry["indices"].append(selection["query_index"])
            purchases, priced_indices = [], set()
            for entry in grouped.values():
                try:
                    priced = purchase(entry["product"], entry["amount"], entry["measure"])
                except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
                    continue
                purchases.append(priced)
                priced_indices.update(entry["indices"])
            missing = [query for index, query in enumerate(queries) if index not in priced_indices]
            return self.price_report(purchases, missing, payload["nutrition_request"]["budget_rub"])
        except Exception:
            return self.skip_cart(failure)

    def render(self, proposal, response, *, keep_plan=False):
        text = proposal["text"]
        if not proposal["products"]:
            return response + "\n\n" + text
        try:
            link = self.call_catalog("cart_link", proposal["products"])
        except Exception:
            self.skip_cart("link_error")
            return (
                response
                + "\n\n"
                + text
                + "\n\nСсылка на корзину недоступна. Найденные цены приведены выше."
            )
        basket = "Корзина ВкусВилла: <" + link + ">\n\n" + text
        return response + "\n\n" + basket if keep_plan else basket
