"""Model Hugging Face chạy ngay trên máy (miễn phí, không cần key, không giới hạn lượt).

- WhisperLocal: nghe giọng -> phụ đề (openai/whisper-large-v3-turbo, Belle-whisper tiếng Trung…)
- HFLocalChat: LLM dịch phụ đề (Qwen2.5 Instruct…), chuẩn Provider.chat như các nhà cung cấp khác

Cần: torch (bản CUDA nếu có card NVIDIA) + transformers. Model tự tải từ Hugging Face lần đầu
vào thư mục cache của Hugging Face (~/.cache/huggingface).
"""
from __future__ import annotations

import threading

from .providers import Provider, ProviderError

WHISPER_MODELS = {
    "Whisper large-v3-turbo (đa ngôn ngữ, nhanh)": "openai/whisper-large-v3-turbo",
    "Belle Whisper turbo (chuyên tiếng Trung)": "BELLE-2/Belle-whisper-large-v3-turbo-zh",
    "Whisper large-v3 (chậm, kỹ hơn)": "openai/whisper-large-v3",
    "Whisper small (máy yếu)": "openai/whisper-small",
}
LANG_CODES = {"Tiếng Trung": "zh", "Tiếng Anh": "en", "Tiếng Nhật": "ja", "Tiếng Hàn": "ko",
              "Tiếng Việt": "vi", "Tiếng Thái": "th", "Tiếng Pháp": "fr", "Tiếng Đức": "de",
              "Tiếng Nga": "ru", "Tiếng Tây Ban Nha": "es"}

_GPU_LOCK = threading.Lock()      # một card -> chạy lần lượt


def _torch():
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as e:
        raise ProviderError("Chưa cài model chạy trên máy. Chạy file cai_long_tieng_gpu.bat "
                             "(máy có card NVIDIA) hoặc cai_long_tieng_cpu.bat rồi mở lại app") from e
    import torch
    return torch


def device_name() -> str:
    torch = _torch()
    if torch.cuda.is_available():
        return f"GPU {torch.cuda.get_device_name(0)}"
    return "CPU"


# ====================================================================== Whisper
class WhisperLocal:
    _cache: dict[str, "WhisperLocal"] = {}

    def __init__(self, model_id: str, log=print):
        self.model_id, self.log = model_id, log
        self.pipe = None
        self.device = ""

    @classmethod
    def get(cls, model_id: str, log=print) -> "WhisperLocal":
        w = cls._cache.get(model_id)
        if w is None:
            w = cls._cache[model_id] = cls(model_id, log)
        w.log = log
        return w

    def load(self):
        if self.pipe is not None:
            return
        torch = _torch()
        from transformers import pipeline
        cuda = torch.cuda.is_available()
        self.device = device_name()
        self.log(f"  Đang nạp {self.model_id} trên {self.device} (lần đầu sẽ tải từ Hugging Face)…")
        self.pipe = pipeline("automatic-speech-recognition", model=self.model_id,
                             torch_dtype=torch.float16 if cuda else torch.float32,
                             device=0 if cuda else "cpu")

    def transcribe(self, audio, lang: str = "Tiếng Trung") -> list[tuple[float, float, str, dict]]:
        """audio: numpy float32 mono 16 kHz. Trả [(bắt đầu, kết thúc, chữ, {})] tính theo giây."""
        self.load()
        gen = {"task": "transcribe"}
        code = LANG_CODES.get(lang)
        if code:
            gen["language"] = code
        with _GPU_LOCK:
            out = self.pipe({"raw": audio, "sampling_rate": 16000}, chunk_length_s=30, batch_size=8,
                            return_timestamps=True, generate_kwargs=gen)
        segs = []
        length = len(audio) / 16000
        for ch in out.get("chunks") or []:
            s, e = ch.get("timestamp") or (None, None)
            txt = (ch.get("text") or "").strip()
            if not txt or s is None:
                continue
            e = e if e is not None else min(length, s + 3)
            segs.append((float(s), float(max(e, s + 0.3)), txt, {}))
        return split_long(segs)


def split_long(segs, max_len: float = 7.0):
    """Whisper hay gộp nhiều câu: tách đoạn quá dài theo dấu câu, chia thời gian theo số chữ."""
    import re
    out = []
    for s, e, txt, meta in segs:
        if e - s <= max_len:
            out.append((s, e, txt, meta)); continue
        parts = [p for p in re.split(r"(?<=[。！？!?；;，,])", txt) if p.strip()]
        if len(parts) < 2:
            out.append((s, e, txt, meta)); continue
        # gom các mảnh thành câu ~ max_len giây
        total = sum(len(p) for p in parts)
        t, buf, blen = s, "", 0
        for i, p in enumerate(parts):
            buf += p; blen += len(p)
            if (e - s) * blen / total >= max_len * 0.6 or i == len(parts) - 1:
                d = (e - s) * blen / total
                out.append((t, t + d, buf.strip().rstrip("，,"), dict(meta)))
                t += d; buf, blen = "", 0
    return out


# ====================================================================== LLM dịch
LOCAL_LLMS = ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-3B-Instruct", "Qwen/Qwen2.5-14B-Instruct",
              "Qwen/Qwen2.5-1.5B-Instruct"]


class _LLM:
    _cache: dict[str, "_LLM"] = {}
    _lock = threading.Lock()

    def __init__(self, model_id: str):
        torch = _torch()
        from transformers import AutoModelForCausalLM, AutoTokenizer
        cuda = torch.cuda.is_available()
        kw = {"torch_dtype": torch.float16 if cuda else torch.float32}
        self.note = ""
        if cuda:
            kw["device_map"] = "cuda"
            vram = torch.cuda.get_device_properties(0).total_memory / 2**30
            big = any(t in model_id for t in ("7B", "8B", "14B", "32B"))
            if big and vram < 20:
                try:
                    from transformers import BitsAndBytesConfig
                    import bitsandbytes  # noqa: F401
                    kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
                                                                   bnb_4bit_quant_type="nf4")
                    self.note = "4-bit"
                except ImportError:
                    kw["device_map"] = "auto"   # tràn sang RAM: chạy được nhưng chậm
                    self.note = "chưa có bitsandbytes, chạy chậm"
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(model_id, **kw)
        self.model.eval()
        self.device = device_name()

    @classmethod
    def get(cls, model_id: str) -> "_LLM":
        with cls._lock:
            m = cls._cache.get(model_id)
            if m is None:
                for old in list(cls._cache):           # chỉ giữ 1 model trong VRAM
                    del cls._cache[old]
                m = cls._cache[model_id] = cls(model_id)
            return m

    def chat(self, system: str, user: str, temperature: float = 0.3, max_new: int = 4096) -> str:
        torch = _torch()
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        ids = self.tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt").to(self.model.device)
        with _GPU_LOCK, torch.inference_mode():
            out = self.model.generate(ids, max_new_tokens=max_new, do_sample=temperature > 0,
                                      temperature=max(temperature, 0.01), top_p=0.9,
                                      pad_token_id=self.tok.eos_token_id)
        return self.tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=True)


class HFLocalChat(Provider):
    """Nhà cung cấp 'Hugging Face trên máy': dịch bằng LLM mở chạy trên card của bạn."""

    def chat(self, system: str, user: str) -> str:
        try:
            llm = _LLM.get(self.model)
        except ProviderError:
            raise
        except OSError as e:
            raise ProviderError(f"Không tải được model {self.model} từ Hugging Face: {e}") from e
        return llm.chat(system, user, self.temperature)

    def list_models(self) -> list[str]:
        return list(LOCAL_LLMS)
