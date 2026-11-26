"""Lớp gọi API cho các nhà cung cấp AI.

- Phần lớn nhà cung cấp (OpenAI/Codex, DeepSeek, Gemini, OpenRouter, Groq...) hỗ trợ
  chuẩn OpenAI "chat/completions" -> dùng chung OpenAICompatible.
- Claude (Anthropic) dùng API "messages" riêng -> AnthropicProvider.
"""
from __future__ import annotations

import json
import threading

import re
import time
import requests

TIMEOUT = 180


def strip_thinking(text: str) -> str:
    """Model suy luận (DeepSeek-R1, Qwen3, GLM…) có thể trả kèm <think>…</think> trước câu trả lời."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    if "</think>" in t:                     # chỉ có thẻ đóng (phần mở bị cắt)
        t = t.split("</think>", 1)[1]
    return t.strip()


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
    "Vertex AI (API key)": {
        "kind": "vertex",
        "base_url": "https://aiplatform.googleapis.com/v1",
        "models": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
    },
    "Vertex AI (Service account JSON)": {
        "kind": "vertex_sa",
        "base_url": "",  # tự tính từ Location + Project ID
        "models": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
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
    "Hugging Face (Inference API)": {
        "kind": "openai",
        "base_url": "https://router.huggingface.co/v1",
        "models": ["deepseek-ai/DeepSeek-V3-0324", "Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen3-235B-A22B-Instruct-2507",
                   "meta-llama/Llama-3.3-70B-Instruct", "moonshotai/Kimi-K2-Instruct"],
    },
    "Hugging Face trên máy (miễn phí)": {
        "kind": "hf_local",
        "base_url": "local",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-3B-Instruct", "Qwen/Qwen2.5-14B-Instruct",
                   "Qwen/Qwen2.5-1.5B-Instruct"],
    },
    # ---- nhà cung cấp có gói MIỄN PHÍ (chuẩn OpenAI). Giới hạn đặt thấp hơn mức nhà cung cấp cho phép
    # để không bị khoá; "rpm" = request/phút cho MỖI key, "concurrency" = số request chạy cùng lúc mỗi key.
    "Groq (miễn phí)": {
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        "free": True, "rpm": 25, "concurrency": 2,
        "signup": "https://console.groq.com/keys",
        "note": "Rất nhanh. Gói miễn phí giới hạn số request và token mỗi phút/ngày.",
    },
    "Cerebras (miễn phí)": {
        "kind": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "models": ["gpt-oss-120b", "qwen-3.8-27b"],
        "free": True, "rpm": 25, "concurrency": 2,
        "signup": "https://cloud.cerebras.ai",
        "note": "Rất nhanh. Gói miễn phí có giới hạn và ngữ cảnh ngắn hơn (~64k).",
    },
    "NVIDIA NIM (miễn phí)": {
        "kind": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["deepseek-ai/deepseek-v3.1"],
        "free": True, "rpm": 35, "concurrency": 3,
        "signup": "https://build.nvidia.com/settings/api-keys",
        "note": "Hơn 100 model (DeepSeek, Qwen, Llama, Kimi…), giới hạn ~40 request/phút. Bấm “Lấy model” để xem đủ.",
    },
    "SiliconFlow (miễn phí)": {
        "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen3-8B", "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", "THUDM/glm-4-9b-chat"],
        "free": True, "rpm": 25, "concurrency": 2,
        "signup": "https://cloud.siliconflow.cn",
        "note": "Nền tảng Trung Quốc, model tiếng Trung tốt. Một số model miễn phí vĩnh viễn; đăng ký cần số điện thoại.",
    },
    "Zhipu GLM (miễn phí)": {
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4.7-flash", "glm-4.5-flash"],
        "free": True, "rpm": 20, "concurrency": 1,
        "signup": "https://open.bigmodel.cn",
        "note": "GLM-Flash miễn phí nhưng chỉ chạy 1 request cùng lúc cho mỗi key.",
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
        self.timeout = TIMEOUT          # giây chờ mỗi request (hộp thoại kiểm tra key đặt ngắn hơn)
        self.label = self.model
        # giới hạn riêng của key này (gói miễn phí): giãn cách request + số request chạy cùng lúc
        self.min_interval = 0.0
        self._sem: threading.Semaphore | None = None
        self._gate = threading.Lock()
        self._next_at = 0.0

    def set_limits(self, rpm: int = 0, concurrency: int = 0):
        self.min_interval = 60.0 / rpm if rpm else 0.0
        self._sem = threading.BoundedSemaphore(concurrency) if concurrency else None

    def _wait_turn(self):
        if not self.min_interval:
            return
        with self._gate:
            now = time.monotonic()
            at = max(now, self._next_at)
            self._next_at = at + self.min_interval
        if at > now:
            time.sleep(at - now)

    def chat(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def list_models(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def _post(self, url, **kw):
        try:
            return self.session.post(url, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise RetryableError(f"Lỗi mạng: {e}") from e

    def _get(self, url, **kw):
        try:
            return self.session.get(url, timeout=min(60, self.timeout), **kw)
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
        if self._sem:
            self._sem.acquire()
        try:
            self._wait_turn()
            resp = self._post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
        finally:
            if self._sem:
                self._sem.release()
        _raise_for(resp)
        data = resp.json()
        try:
            return strip_thinking(data["choices"][0]["message"]["content"] or "")
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


class VertexProvider(Provider):
    """Vertex AI bằng API key (cả key chế độ Express): endpoint generateContent toàn cầu."""

    SAFETY = [{"category": c, "threshold": "OFF"} for c in (
        "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT")]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.use_safety = True  # phim đánh nhau dễ bị chặn nhầm -> tắt bộ lọc nếu được phép

    def _url(self, model: str) -> str:
        return f"{self.base_url}/{model}:generateContent"

    def _auth(self) -> tuple[dict, dict]:
        """(params, headers) xác thực."""
        return {"key": self.api_key}, {}

    def chat(self, system: str, user: str) -> str:
        return self.generate([{"text": user}], system=system)

    def generate(self, parts: list[dict], system: str = "", json_mode: bool = False,
                 timeout: int = 300) -> str:
        """Gửi nhiều phần (chữ, âm thanh inlineData...) -> chữ trả về."""
        model = self.model.removeprefix("google/")
        if not model.startswith("publishers/"):
            model = f"publishers/google/models/{model}"
        params, headers = self._auth()
        cfg = {"temperature": self.temperature}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": cfg,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        sent_safety = self.use_safety
        if sent_safety:
            body["safetySettings"] = self.SAFETY
        resp = self._post(self._url(model), params=params, headers=headers, json=body)  # noqa
        if resp.status_code == 400 and sent_safety and "safety" in resp.text.lower():
            self.use_safety = False  # tài khoản không cho tắt bộ lọc -> gửi lại không kèm
            raise RetryableError("Vertex không cho tắt bộ lọc an toàn, gửi lại với mặc định", 0.1)
        _raise_for(resp)
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "không rõ")
            raise RetryableError(f"Vertex chặn nội dung ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise RetryableError(f"Vertex trả về rỗng (finishReason={cands[0].get('finishReason')})")
        return text

    def list_models(self) -> list[str]:
        raise ProviderError("Vertex API key không có lệnh liệt kê model — hãy gõ tên model "
                            "(vd: gemini-2.5-flash) vào ô Mô hình")


class _SATokens:
    """Đổi file service account JSON -> access token (cache, tự làm mới, dùng chung mọi luồng)."""
    _cache: dict[str, "_SATokens"] = {}
    _cache_lock = threading.Lock()

    def __init__(self, path: str):
        try:
            from google.oauth2 import service_account
        except ImportError as e:  # pragma: no cover
            raise ProviderError("Thiếu thư viện google-auth: chạy  pip install google-auth") from e
        try:
            self.info = json.load(open(path, encoding="utf-8"))
            self.creds = service_account.Credentials.from_service_account_info(
                self.info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except FileNotFoundError:
            raise ProviderError(f"Không thấy file service account: {path}")
        except (ValueError, KeyError) as e:
            raise ProviderError(f"File JSON không phải service account hợp lệ: {e}")
        self.lock = threading.Lock()

    @classmethod
    def get(cls, path: str) -> "_SATokens":
        with cls._cache_lock:
            if path not in cls._cache:
                cls._cache[path] = cls(path)
            return cls._cache[path]

    def token(self) -> str:
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        with self.lock:
            if not self.creds.valid:  # hết hạn hoặc sắp hết (google-auth tự trừ hao)
                try:
                    self.creds.refresh(Request())
                except RefreshError as e:
                    raise ProviderError(f"Service account bị từ chối: {e}")
                except TransportError as e:
                    raise RetryableError(f"Lỗi mạng khi lấy token: {e}")
            return self.creds.token


def project_from_json(path: str) -> str:
    try:
        return json.load(open(path, encoding="utf-8")).get("project_id", "")
    except (OSError, ValueError):
        return ""


class VertexSAProvider(VertexProvider):
    """Vertex AI bằng file service account JSON + Project ID + Location."""

    def __init__(self, json_path: str, model: str, base_url: str, project: str = "", location: str = ""):
        super().__init__(json_path, model, base_url)
        self.tokens = _SATokens.get(self.api_key)
        self.project = project.strip() or self.tokens.info.get("project_id", "")
        self.location = (location or "global").strip()
        if not self.project:
            raise ProviderError("Chưa có Project ID (file JSON cũng không ghi project_id)")
        if not base_url:
            host = ("aiplatform.googleapis.com" if self.location == "global"
                    else f"{self.location}-aiplatform.googleapis.com")
            self.base_url = f"https://{host}/v1"
        self.base_url = f"{self.base_url}/projects/{self.project}/locations/{self.location}"

    def _auth(self):
        return {}, {"Authorization": f"Bearer {self.tokens.token()}"}

    def generate(self, *a, **kw) -> str:
        try:
            return super().generate(*a, **kw)
        except ProviderError as e:
            if "404" in str(e)[:12] and self.location != "global":
                raise ProviderError(f"{e}\n→ Model này không có ở vùng {self.location}. "
                                    "Thử đổi Location sang 'global'.") from e
            raise

    def list_models(self) -> list[str]:
        raise ProviderError("Hãy gõ tên model Gemini (vd: gemini-2.5-flash) vào ô Mô hình")


# Model đã bị nhà cung cấp tắt -> model thay thế họ khuyên dùng (cấu hình cũ tự chuyển, không báo 404 nữa).
# Groq: console.groq.com/docs/deprecations (Llama 3.x tắt 16/8/2026, Qwen3.6 27B tắt 14/9/2026).
RETIRED = {
    "Groq (miễn phí)": {
        "llama-3.3-70b-versatile": "openai/gpt-oss-120b", "llama-3.1-8b-instant": "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b": "openai/gpt-oss-120b", "qwen/qwen3-32b": "openai/gpt-oss-120b",
        "meta-llama/llama-4-scout-17b-16e-instruct": "openai/gpt-oss-120b",
        "meta-llama/llama-4-maverick-17b-128e-instruct": "openai/gpt-oss-120b",
        "moonshotai/kimi-k2-instruct-0905": "openai/gpt-oss-120b", "gemma2-9b-it": "openai/gpt-oss-20b",
    },
    "Cerebras (miễn phí)": {
        "llama3.1-8b": "gpt-oss-120b", "llama-3.3-70b": "gpt-oss-120b", "zai-glm-4.7": "gpt-oss-120b",
        "zai-glm-4.6": "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507": "gpt-oss-120b",
    },
}


def current_model(name: str, model: str) -> str:
    """Đổi model đã bị tắt sang model thay thế."""
    return RETIRED.get(name, {}).get(model or "", model)


def model_gone(err: Exception | str) -> bool:
    """Lỗi kiểu “model không tồn tại / đã ngừng” (thử model khác), khác với lỗi key/hết lượt."""
    t = str(err).lower()
    return ("404" in t or "400" in t) and any(w in t for w in (
        "does not exist", "not found", "model_not_found", "decommissioned", "deprecated", "no such model",
        "not supported", "model not exist", "模型不存在"))


def make_provider(name: str, api_key: str, model: str, base_url: str | None = None,
                  project: str = "", location: str = "") -> Provider:
    cfg = PROVIDERS[name]
    model = current_model(name, model)
    if cfg["kind"] == "vertex_sa":
        prov = VertexSAProvider(api_key, model, (base_url or "").strip(), project, location)
        prov.label = f"Vertex AI · {model} · {prov.project}"
        return prov
    if cfg["kind"] == "hf_local":
        from .hf_local import HFLocalChat
        prov = HFLocalChat("", model, "local")
        prov.label = f"Máy mình · {model.split('/')[-1]}"
        return prov
    url = (base_url or cfg["base_url"]).strip()
    if not url:
        raise ProviderError("Chưa nhập Base URL cho nhà cung cấp tùy chỉnh")
    cls = {"anthropic": AnthropicProvider, "vertex": VertexProvider}.get(cfg["kind"], OpenAICompatible)
    prov = cls(api_key, model, url)
    prov.set_limits(cfg.get("rpm", 0), cfg.get("concurrency", 0))
    tail = api_key.strip()[-4:] if api_key.strip() else "----"
    prov.label = f"{name.split(' (')[0]} · {model} · key …{tail}"
    return prov
