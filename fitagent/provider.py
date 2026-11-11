"""Direct OpenCode Go chat-completions transport; never logs request bodies."""

import json

import httpx

from fitagent.tokens import api_usage


class ModelClient:
    def __init__(self, key, url, model, session_id, audit):
        self.key, self.url, self.model = key, url, model
        self.session_id, self.audit = session_id, audit
        if not url.startswith("https://"):
            raise ValueError("API URL должен использовать HTTPS.")

    def generate(self, messages, max_tokens, purpose):
        if not self.key:
            raise ValueError("Заполните OPENCODE_API_KEY в .env и перезапустите fitagent.")
        self.audit(
            "api_request", {"purpose": purpose, "model": self.model, "output_limit": max_tokens}
        )
        payload = {"model": self.model, "messages": messages, "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        try:
            with httpx.Client(timeout=httpx.Timeout(180, connect=15)) as client:
                response = client.post(
                    self.url,
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
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("Ответ обрезан лимитом модели; результат не сохранён как полный.")
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("API вернул пустой ответ.")
            return text.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.audit("api_error", {"purpose": purpose, "type": type(exc).__name__})
            raise ValueError(
                "Ошибка связи или формата API. Попробуйте ещё раз; детали в /logs."
            ) from None
