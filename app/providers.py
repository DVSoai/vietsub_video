"""Lớp gọi API cho các nhà cung cấp AI.

- Phần lớn nhà cung cấp (OpenAI/Codex, DeepSeek, Gemini, OpenRouter, Groq...) hỗ trợ
  chuẩn OpenAI "chat/completions" -> dùng chung OpenAICompatible.
- Claude (Anthropic) dùng API "messages" riêng -> AnthropicProvider.
"""
from __future__ import annotations

import requests

TIMEOUT = 180


class ProviderError(Exception):
    """Lỗi không nên thử lại (sai key, sai model...)."""


class RetryableError(Exception):
    """Lỗi tạm thời (429, 5xx, mạng) -> thử lại."""

    def __init__(self, msg: str, wait: float | None = None):
        super().__init__(msg)
        self.wait = wait


# Tên hiển thị -> cấu hình. "models" chỉ là gợi ý; bấm "Lấy model" để lấy danh sách thật.
PROVIDERS: dict[str, dict] = {
    "Gemini": {
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "Claude (Anthropic)": {
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
    },
    "OpenAI (Codex / ChatGPT)": {
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": [],
    },
    "DeepSeek": {
        "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "OpenRouter": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": [],
    },
    "Tùy chỉnh (chuẩn OpenAI)": {
        "kind": "openai",
        "base_url": "",
        "models": [],
    },
}


def _raise_for(resp: requests.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json()
        detail = detail.get("error", detail)
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
    except ValueError:
        detail = resp.text[:300]
    msg = f"HTTP {resp.status_code}: {detail}"
    if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
        wait = None
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                wait = float(ra)
            except ValueError:
                pass
        raise RetryableError(msg, wait)
    raise ProviderError(msg)


class Provider:
    def __init__(self, api_key: str, model: str, base_url: str, temperature: float = 0.3):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.session = requests.Session()

    def chat(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def list_models(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def _post(self, url, **kw):
        try:
            return self.session.post(url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise RetryableError(f"Lỗi mạng: {e}") from e

    def _get(self, url, **kw):
        try:
            return self.session.get(url, timeout=60, **kw)
        except requests.RequestException as e:
            raise ProviderError(f"Lỗi mạng: {e}") from e


class OpenAICompatible(Provider):
    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def chat(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
        }
        resp = self._post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
        _raise_for(resp)
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RetryableError(f"Phản hồi lạ: {str(data)[:300]}")

    def list_models(self) -> list[str]:
        resp = self._get(f"{self.base_url}/models", headers=self._headers())
        _raise_for(resp)
        items = resp.json().get("data", [])
        ids = [m.get("id", "") for m in items if m.get("id")]
        return sorted(i.removeprefix("models/") for i in ids)


class AnthropicProvider(Provider):
    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def chat(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "max_tokens": 8192,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": self.temperature,
        }
        resp = self._post(f"{self.base_url}/messages", headers=self._headers(), json=body)
        _raise_for(resp)
        data = resp.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if not parts:
            raise RetryableError(f"Phản hồi lạ: {str(data)[:300]}")
        return "".join(parts)

    def list_models(self) -> list[str]:
        resp = self._get(f"{self.base_url}/models?limit=100", headers=self._headers())
        _raise_for(resp)
        return [m["id"] for m in resp.json().get("data", []) if m.get("id")]


def make_provider(name: str, api_key: str, model: str, base_url: str | None = None) -> Provider:
    cfg = PROVIDERS[name]
    url = (base_url or cfg["base_url"]).strip()
    if not url:
        raise ProviderError("Chưa nhập Base URL cho nhà cung cấp tùy chỉnh")
    cls = AnthropicProvider if cfg["kind"] == "anthropic" else OpenAICompatible
    return cls(api_key, model, url)
