"""Nutrition routing and bounded anonymous shopping, independent of the terminal."""

import json
import re

from fitagent.memory_schema import unique_object, validate_shape
from fitagent.nutrition_schema import (
    CATALOG_QUERY_SCHEMA,
    PLAN_FIELDS_PROMPT,
    PLAN_FIELDS_SCHEMA,
    QUERY_PROMPT,
    QUERY_SCHEMA,
    READY_QUERY_SCHEMA,
    ROUTE_PROMPT,
    ROUTE_SCHEMA,
    SELECTION_PROMPT,
    SELECTION_SCHEMA,
    UNMAPPED_TERM,
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

    def price_report(self, purchases, missing, budget, *, audit=True):
        total = sum(item["total"] for item in purchases)
        over_budget = budget is not None and total > number(budget)
        reason = "incomplete_selection" if missing else "budget" if over_budget else None
        if reason and audit:
            self.skip_cart(reason)
        return {
            "reason": reason,
            "text": render_purchases(
                purchases, missing=missing, budget=budget if over_budget else None
            ),
            "products": []
            if reason
            else [{"id": item["id"], "q": item["quantity"]} for item in purchases],
            "evidence": json.dumps(purchases, ensure_ascii=False, default=str),
        }

    def structured(self, prompt, schema, payload, purpose):
        failure = "provider_response"
        try:
            raw = self.client.generate_structured(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                schema,
                purpose=purpose,
            )
            failure = "invalid_json"
            value = json.loads(raw, object_pairs_hook=unique_object)
            failure = "invalid_schema"
            validate_shape(value, schema)
            return value
        except Exception:
            if purpose == "nutrition_selection":
                self.audit("nutrition_selection_failed", {"reason": failure})
            raise

    def selection_fallback(self, catalog, reason=None):
        if reason:
            self.audit("nutrition_selection_failed", {"reason": reason})
        if catalog and any(catalog.values()):
            return {
                "reason": "selection_error",
                "text": "Не удалось сопоставить исходные блюда с товарами. Карточки каталога получены.",
                "products": [],
                "evidence": "[]",
            }
        return self.skip_cart("selection_error")

    def plan_route(self, payload, pending):
        try:
            fields = self.structured(
                PLAN_FIELDS_PROMPT,
                PLAN_FIELDS_SCHEMA,
                {**payload, "pending_menu": pending},
                "nutrition_plan_fields",
            )
        except Exception:
            raise ValueError(NUTRITION_ERROR) from None
        continues = fields.pop("continues_menu")
        if pending and not continues:
            return None
        quote = fields.pop("goal_change_quote").strip()
        goal_change = bool(quote and quote.casefold() in payload["question"].casefold())
        if pending:
            if not fields["period"].strip():
                fields["period"] = pending["period"]
            if fields["food_format"] == "unknown":
                fields["food_format"] = pending["food_format"]
            goal_change = goal_change or pending["goal_related"]
        question = ""
        if not fields["period"].strip():
            question = "На какой срок подобрать меню?"
        elif fields["food_format"] == "unknown":
            question = "Подобрать готовую еду или продукты для готовки?"
        return dict(
            fields,
            action="clarify" if question else "shop",
            question=question,
            goal_related=goal_change,
        )

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

    def search_products(self, query, cheaper):
        items = self.call_catalog("search", query, cheaper=cheaper)
        if len(items) > 5:
            raise ValueError(MCP_ERROR)
        products = []
        for item in items:
            try:
                products.append(product_data(item))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return products

    def prepare(self, payload, diet, *, catalog_terms_only=False, catalog=None):
        """Return priced matches even if a basket cannot be created."""
        failure = "selection_error"
        try:
            schema = CATALOG_QUERY_SCHEMA if catalog_terms_only else QUERY_SCHEMA
            prompt = QUERY_PROMPT
            if catalog_terms_only:
                if payload["nutrition_request"]["food_format"] == "ready":
                    schema = READY_QUERY_SCHEMA
                prompt += (
                    " Для каждой обязательной позиции выбери разрешённую поисковую категорию. "
                    "Для готового блюда ищи категорию блюда целиком: "
                    "для курицы с гречкой — курица с гречкой. Не разбирай блюдо на ингредиенты. Подходящий готовый товар будет "
                    "проверен по карточке. При отсутствии категории добавь отметку "
                    f"«{UNMAPPED_TERM}», сохрани остальные категории. Объедини повторы."
                )
            queries = self.structured(
                prompt,
                schema,
                {**payload, "recommended_diet": diet},
                "nutrition_queries",
            )["queries"]
            unmapped = []
            if catalog_terms_only:
                if UNMAPPED_TERM in queries:
                    unmapped.append("позиции меню без подходящей поисковой категории")
                queries = list(dict.fromkeys(query for query in queries if query != UNMAPPED_TERM))
            if len(set(queries)) != len(queries) or any(
                not re.fullmatch(r"[а-яёА-ЯЁ][а-яёА-ЯЁ -]{0,79}", query) for query in queries
            ):
                return self.skip_cart("invalid_queries")
            candidates = []
            for index, query in enumerate(queries):
                failure = "mcp_error"
                products = self.search_products(query, payload["nutrition_request"]["cheaper"])
                if catalog is not None:
                    catalog[query] = products
                candidates.append({"query_index": index, "query": query, "candidates": products})
            searchable = [group for group in candidates if group["candidates"]]
            if not searchable:
                return self.price_report([], [*queries, *unmapped], None, audit=catalog is None)
            failure = "selection_error"
            try:
                chosen = self.structured(
                    SELECTION_PROMPT,
                    SELECTION_SCHEMA,
                    {**payload, "recommended_diet": diet, "catalog": searchable},
                    "nutrition_selection",
                )["items"]
            except Exception:
                return self.selection_fallback(catalog)
            indices = {item["query_index"] for item in chosen}
            if len(indices) != len(chosen):
                return self.selection_fallback(catalog, "duplicate_group")
            if not indices <= {group["query_index"] for group in searchable}:
                return self.selection_fallback(catalog, "unknown_group")
            failure = "quantity_error"
            grouped = {}
            for selection in chosen:
                amount = number(selection["amount"])
                source = candidates[selection["query_index"]]["candidates"]
                product = next((item for item in source if item["id"] == selection["id"]), None)
                if product is None:
                    return self.selection_fallback(catalog, "unknown_product")
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
            return self.price_report(
                purchases,
                missing + unmapped,
                payload["nutrition_request"]["budget_rub"],
                audit=catalog is None,
            )
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
