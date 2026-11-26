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
    "Qwen3-ASR 1.7B (tiếng Trung tốt nhất, căn giờ từng chữ)": "Qwen/Qwen3-ASR-1.7B",
    "Qwen3-ASR 0.6B (nhẹ, nhanh)": "Qwen/Qwen3-ASR-0.6B",
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


# ====================================================================== Qwen3-ASR + ForcedAligner
QWEN_LANG = {"Tiếng Trung": "Chinese", "Tiếng Anh": "English", "Tiếng Nhật": "Japanese", "Tiếng Hàn": "Korean",
             "Tiếng Việt": "Vietnamese", "Tiếng Thái": "Thai", "Tiếng Pháp": "French", "Tiếng Đức": "German",
             "Tiếng Nga": "Russian", "Tiếng Tây Ban Nha": "Spanish"}
ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"
SENT_END = "。！？!?；;…"
SOFT_END = "，,、：:"


def group_words(items: list[tuple[str, float, float]], full_text: str, max_len: float = 6.5,
                gap: float = 0.6) -> list[tuple[float, float, str, dict]]:
    """Gom các chữ/từ có thời gian (của ForcedAligner) thành câu phụ đề.

    items: [(chữ, bắt đầu, kết thúc)] không có dấu câu; full_text: câu nhận được có dấu câu.
    Dấu câu trong full_text được gắn lại vào chữ đứng trước nó rồi dùng để ngắt câu."""
    if not items:
        return []
    # gắn dấu câu từ full_text vào từng chữ
    pos, words = 0, []
    for txt, s, e in items:
        txt = (txt or "").strip()
        if not txt:
            continue
        k = full_text.find(txt, pos)
        if k >= 0 and words:
            between = full_text[pos:k].strip()
            if between:
                words[-1][0] += between
        if k >= 0:
            pos = k + len(txt)
        words.append([txt, float(s), float(e)])
    if words and pos < len(full_text):
        words[-1][0] += full_text[pos:].strip()

    def joiner(a: str, b: str) -> str:
        cjk = lambda ch: "\u3000" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af"  # noqa: E731
        return "" if (a and cjk(a[-1])) or (b and cjk(b[0])) else " "
    segs, cur = [], []
    for i, w in enumerate(words):
        if cur and (w[1] - cur[-1][2] > gap):
            segs.append(cur); cur = []
        cur.append(w)
        dur = cur[-1][2] - cur[0][1]
        last = w[0][-1] if w[0] else ""
        if last in SENT_END or (last in SOFT_END and dur > 3.5) or dur > max_len:
            segs.append(cur); cur = []
    if cur:
        segs.append(cur)
    out = []
    for seg in segs:
        text = seg[0][0]
        for a, b in zip(seg, seg[1:]):
            text += joiner(a[0], b[0]) + b[0]
        text = text.strip().rstrip("，,、")
        if text:
            out.append((seg[0][1], max(seg[-1][2], seg[0][1] + 0.3), text, {}))
    return out


class QwenASRLocal:
    _cache: dict[str, "QwenASRLocal"] = {}
    WINDOW = 240.0          # ForcedAligner căn được tối đa ~5 phút mỗi lần -> cắt 4 phút cho an toàn

    def __init__(self, model_id: str, log=print):
        self.model_id, self.log = model_id, log
        self.model = None
        self.device = ""

    @classmethod
    def get(cls, model_id: str, log=print) -> "QwenASRLocal":
        m = cls._cache.get(model_id)
        if m is None:
            m = cls._cache[model_id] = cls(model_id, log)
        m.log = log
        return m

    def load(self):
        if self.model is not None:
            return
        torch = _torch()
        try:
            from qwen_asr import Qwen3ASRModel
        except ImportError as e:
            raise ProviderError("Chưa cài Qwen3-ASR. Chạy cai_long_tieng_gpu.bat (đã gồm qwen-asr) rồi mở lại app") from e
        cuda = torch.cuda.is_available()
        self.device = device_name()
        self.log(f"  Đang nạp {self.model_id} + {ALIGNER.split('/')[-1]} trên {self.device} "
                 "(lần đầu sẽ tải từ Hugging Face)…")
        dtype = torch.bfloat16 if cuda else torch.float32
        dev = "cuda:0" if cuda else "cpu"
        self.model = Qwen3ASRModel.from_pretrained(
            self.model_id, dtype=dtype, device_map=dev, max_inference_batch_size=4, max_new_tokens=1024,
            forced_aligner=ALIGNER, forced_aligner_kwargs=dict(dtype=dtype, device_map=dev))

    def transcribe(self, audio, lang: str = "Tiếng Trung") -> list[tuple[float, float, str, dict]]:
        """audio: numpy float32 mono 16 kHz -> [(bắt đầu, kết thúc, chữ, {})] tính theo giây."""
        self.load()
        sr, n = 16000, len(audio)
        win = int(self.WINDOW * sr)
        out = []
        for off in range(0, max(n, 1), win):
            piece = audio[off:off + win]
            if len(piece) < sr * 0.3:
                continue
            with _GPU_LOCK:
                res = self.model.transcribe(audio=[(piece, sr)], language=[QWEN_LANG.get(lang)],
                                            return_time_stamps=True)
            r = res[0]
            items = [(getattr(t, "text", ""), getattr(t, "start_time", 0.0), getattr(t, "end_time", 0.0))
                     for t in (getattr(r, "time_stamps", None) or [])]
            base = off / sr
            for s, e, txt, meta in group_words(items, getattr(r, "text", "") or ""):
                out.append((s + base, e + base, txt, meta))
        return out


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
