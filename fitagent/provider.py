"""Direct Go transports: Flash chat completions and strict Luna Responses."""

import json

import httpx

from fitagent.safety_schema import SafetyValidationError
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

    def generate_structured(self, messages, schema, purpose="memory_extraction"):
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
                    "name": "safety_check" if purpose.startswith("safety_") else "sticky_facts",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        data = self._post(MEMORY_URL, payload, purpose, MEMORY_MODEL, MEMORY_OUTPUT_LIMIT)
        failure_reason = "response_protocol"
        try:
            if data["status"] != "completed":
                failure_reason = "response_incomplete"
                incomplete = data.get("incomplete_details")
                if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
                    failure_reason = "output_limit"
                raise ValueError
            texts = []
            for item in data["output"]:
                if item["type"] == "reasoning":
                    continue
                if item["type"] != "message" or item["role"] != "assistant":
                    raise ValueError
                if item["status"] != "completed":
                    failure_reason = "response_incomplete"
                    raise ValueError
                for part in item["content"]:
                    if part["type"] == "refusal":
                        failure_reason = "response_refusal"
                        raise ValueError
                    if part["type"] != "output_text" or not isinstance(part["text"], str):
                        raise ValueError
                    texts.append(part["text"])
            text = "".join(texts).strip()
            if not text:
                failure_reason = "response_empty"
                raise ValueError
            return text
        except (ValueError, KeyError, IndexError, TypeError):
            if purpose.startswith("safety_"):
                raise SafetyValidationError(failure_reason) from None
            raise ValueError(
                "Luna не вернула полный структурированный ответ. Память не обновлена; повторите вопрос."
            ) from None

    def generate_safety(self, messages, stage):
        from fitagent.safety_schema import schema_from_safety_messages

        return self.generate_structured(
            messages, schema_from_safety_messages(messages), purpose="safety_" + stage
        )

    def _post(self, url, payload, purpose, model, output_limit):
        if not self.key:
            if purpose.startswith("safety_"):
                raise SafetyValidationError("missing_api_key")
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
                if purpose.startswith("safety_"):
                    raise SafetyValidationError("http_error", http_status=response.status_code)
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
            if purpose.startswith("safety_"):
                reason = "response_protocol"
                if isinstance(exc, httpx.TimeoutException):
                    reason = "request_timeout"
                elif isinstance(exc, httpx.HTTPError):
                    reason = "network_error"
                elif isinstance(exc, json.JSONDecodeError):
                    reason = "invalid_json"
                raise SafetyValidationError(reason) from None
            raise ValueError(
                "Не удалось получить ответ сервиса. Повторите запрос; проверьте подключение к интернету."
            ) from None
