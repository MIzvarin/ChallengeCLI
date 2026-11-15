"""Direct Go transports: Flash chat completions and strict Luna Responses."""

import json

import httpx

from fitagent.tokens import api_usage

MEMORY_URL = "https://opencode.ai/inference/go/openai/v1/responses"
MEMORY_MODEL = "gpt-6-luna"
MEMORY_OUTPUT_LIMIT = 8192


class ModelClient:
    def __init__(self, key, url, model, session_id, audit):
        self.key, self.url, self.model = key, url, model
        self.session_id, self.audit = session_id, audit
        if not url.startswith("https://"):
            raise ValueError("API URL должен использовать HTTPS.")

    def generate(self, messages, max_tokens, purpose):
        payload = {"model": self.model, "messages": messages, "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data = self._post(self.url, payload, purpose, self.model, max_tokens)
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("Ответ обрезан лимитом модели; результат не сохранён как полный.")
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("API вернул пустой ответ.")
            return text.strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ValueError("API вернул неверный формат ответа. Повторите запрос.") from None

    def generate_structured(self, messages, schema):
        """No fallback to JSON mode/plain text and no automatic paid retries."""
        payload = {
            "model": MEMORY_MODEL,
            "input": messages,
            "stream": False,
            "store": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": MEMORY_OUTPUT_LIMIT,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sticky_facts",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        data = self._post(
            MEMORY_URL, payload, "memory_extraction", MEMORY_MODEL, MEMORY_OUTPUT_LIMIT
        )
        try:
            if data["status"] != "completed":
                raise ValueError
            texts = []
            for item in data["output"]:
                if item["type"] == "reasoning":
                    continue
                if (
                    item["type"] != "message"
                    or item["role"] != "assistant"
                    or item["status"] != "completed"
                ):
                    raise ValueError
                for part in item["content"]:
                    if part["type"] != "output_text" or not isinstance(part["text"], str):
                        raise ValueError
                    texts.append(part["text"])
            text = "".join(texts).strip()
            if not text:
                raise ValueError
            return text
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValueError(
                "Luna не вернула полный структурированный ответ. Память не обновлена; повторите вопрос."
            ) from None

    def _post(self, url, payload, purpose, model, output_limit):
        if not self.key:
            raise ValueError("Заполните OPENCODE_API_KEY в .env и перезапустите fitagent.")
        self.audit(
            "api_request", {"purpose": purpose, "model": model, "output_limit": output_limit}
        )
        try:
            with httpx.Client(timeout=httpx.Timeout(180, connect=15)) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.key}",
                        "User-Agent": "fitagent/0.1.0",
                        "x-opencode-session": self.session_id,
                    },
                    json=payload,
                )
            if response.status_code != 200:
                self.audit("api_error", {"purpose": purpose, "status": response.status_code})
                raise ValueError(
                    f"API вернул HTTP {response.status_code}. Проверьте ключ и лимиты."
                )
            data = response.json()
            self.audit("api_usage", {"purpose": purpose, **api_usage(data)})
            if not isinstance(data, dict):
                raise TypeError
            return data
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.audit("api_error", {"purpose": purpose, "type": type(exc).__name__})
            raise ValueError(
                "Не удалось получить ответ сервиса. Повторите запрос; проверьте подключение к интернету."
            ) from None
