"""Bộ máy dịch: chia batch, chạy song song, giới hạn RPM, thử lại, dịch bù."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .providers import Provider, ProviderError, RetryableError
from .srt import Cue, has_cjk

CJK_LANGS = {"Tiếng Trung", "Tiếng Nhật", "Tiếng Hàn"}


@dataclass
class Settings:
    source_lang: str = "Tiếng Trung"
    target_lang: str = "Tiếng Việt"
    threads: int = 10
    batch_size: int = 40
    rpm: int = 0  # 0 = không giới hạn
    max_retries: int = 5
    custom_prompt: str = ""


class Stopped(Exception):
    pass


class RateLimiter:
    """Giãn cách các request để không vượt quá `rpm` request/phút (dùng chung mọi luồng)."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self, stop: threading.Event):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        delay = at - time.monotonic()
        if delay > 0 and stop.wait(delay):
            raise Stopped()


def build_system_prompt(s: Settings) -> str:
    base = (
        f"Bạn là dịch giả phụ đề phim chuyên nghiệp. Dịch phụ đề từ {s.source_lang} sang {s.target_lang}.\n"
        "Yêu cầu:\n"
        "- Dịch tự nhiên, ngắn gọn, đúng văn nói của phim, giữ nhất quán tên riêng và xưng hô.\n"
        "- Mỗi mục dịch riêng, KHÔNG gộp hay tách mục, KHÔNG bỏ sót mục nào.\n"
        "- Giữ nguyên ký hiệu xuống dòng \\n và các thẻ như <i>, {\\an8} nếu có.\n"
        f"- Bản dịch không được còn chữ của ngôn ngữ gốc ({s.source_lang}).\n"
        "- Đầu vào là một JSON object {\"id\": \"câu gốc\"}. Chỉ trả về một JSON object "
        "có ĐÚNG các id đó, giá trị là câu đã dịch. Không giải thích, không markdown."
    )
    if s.custom_prompt.strip():
        base += "\n\nHướng dẫn riêng cho phim này (ưu tiên làm theo):\n" + s.custom_prompt.strip()
    return base


def parse_json_reply(text: str) -> dict[str, str]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("không thấy JSON")
    obj = json.loads(t[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON không phải object")
    return {str(k).strip(): ("" if v is None else str(v)) for k, v in obj.items()}


def needs_retranslation(src: str, dst: str | None, source_lang: str) -> bool:
    if dst is None or not dst.strip():
        return bool(src.strip())
    if source_lang in CJK_LANGS and has_cjk(dst):
        return True
    if dst.strip() == src.strip() and re.search(r"\w", src) and not src.strip().isdigit():
        return True
    return False


@dataclass
class Callbacks:
    log: Callable[[str], None] = print
    progress: Callable[[int, int], None] = lambda done, total: None  # batch đã xong / tổng


@dataclass
class Translator:
    providers: list[Provider]
    settings: Settings
    stop: threading.Event = field(default_factory=threading.Event)
    cb: Callbacks = field(default_factory=Callbacks)

    def __post_init__(self):
        if not isinstance(self.providers, list):
            self.providers = [self.providers]
        self.limiter = RateLimiter(self.settings.rpm)
        self.system = build_system_prompt(self.settings)
        self._rr = 0
        self._rr_lock = threading.Lock()
        self._dead: set[int] = set()

    def _alive(self) -> list[Provider]:
        return [p for p in self.providers if id(p) not in self._dead]

    def _next_provider(self) -> Provider:
        with self._rr_lock:
            alive = self._alive()
            if not alive:
                raise ProviderError("Tất cả API key / nhà cung cấp đều lỗi")
            p = alive[self._rr % len(alive)]
            self._rr += 1
            return p

    def _drop(self, prov: Provider, err: Exception) -> bool:
        """Loại provider lỗi hẳn (sai key, sai model...). Trả về True nếu vẫn còn provider khác."""
        with self._rr_lock:
            if id(prov) not in self._dead:
                self._dead.add(id(prov))
                left = len(self._alive())
                self.cb.log(f"  ✖ Loại {getattr(prov, 'label', '?')}: {str(err)[:160]}"
                            + (f" → còn {left} nguồn dịch" if left else ""))
            return bool(self._alive())

    # ---- một batch ----
    def _translate_batch(self, items: dict[str, str]) -> dict[str, str]:
        user = json.dumps(items, ensure_ascii=False)
        last_err = None
        for attempt in range(1, self.settings.max_retries + 1):
            if self.stop.is_set():
                raise Stopped()
            self.limiter.wait(self.stop)
            prov = self._next_provider()
            try:
                reply = prov.chat(self.system, user)
                result = parse_json_reply(reply)
                got = {k: result[k] for k in items if k in result}
                if len(got) >= max(1, int(len(items) * 0.5)):
                    return got  # thiếu vài câu thì để vòng dịch bù xử lý
                last_err = f"chỉ nhận {len(got)}/{len(items)} câu"
            except ProviderError as e:
                if self._drop(prov, e):
                    continue  # thử lại batch này bằng nguồn khác
                raise
            except RetryableError as e:
                last_err = str(e)
                wait = e.wait or min(60, 2 ** attempt + random.random())
                self.cb.log(f"  ⚠ {last_err[:150]} → chờ {wait:.0f}s thử lại ({attempt}/{self.settings.max_retries})")
                if self.stop.wait(wait):
                    raise Stopped()
                continue
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"JSON lỗi: {e}"
            self.cb.log(f"  ⚠ {last_err} → thử lại ({attempt}/{self.settings.max_retries})")
            if self.stop.wait(min(20, attempt * 2)):
                raise Stopped()
        self.cb.log(f"  ✖ Bỏ qua batch sau {self.settings.max_retries} lần: {last_err}")
        return {}

    # ---- chạy nhiều batch song song ----
    def _run(self, items: dict[str, str], batch_size: int, done_offset=0, total=None) -> dict[str, str]:
        keys = list(items)
        batches = [
            {k: items[k] for k in keys[i : i + batch_size]} for i in range(0, len(keys), batch_size)
        ]
        total = total or len(batches)
        out: dict[str, str] = {}
        done = done_offset
        self.cb.progress(done, total)
        with ThreadPoolExecutor(max_workers=max(1, self.settings.threads)) as ex:
            futs = [ex.submit(self._translate_batch, b) for b in batches]
            try:
                for f in as_completed(futs):
                    out.update(f.result())
                    done += 1
                    self.cb.progress(done, total)
            except BaseException:
                self.stop.set()
                for f in futs:
                    f.cancel()
                raise
        return out

    def translate_cues(self, cues: list[Cue], only_ids: list[int] | None = None,
                       existing: dict[int, str] | None = None) -> dict[int, str]:
        """Dịch cues. Trả về {index_cue: bản dịch}. `only_ids` để dịch lại một phần."""
        targets = [c for c in cues if (only_ids is None or c.index in only_ids) and c.text.strip()]
        items = {str(c.index): c.text for c in targets}
        result: dict[int, str] = dict(existing or {})
        if not items:
            return result
        bs = max(1, self.settings.batch_size)
        n_batches = -(-len(items) // bs)
        self.cb.log(f"Dịch {len(items)} câu trong {n_batches} batch, {self.settings.threads} luồng")
        got = self._run(items, bs)
        result.update({int(k): v for k, v in got.items()})

        # Vòng dịch bù: câu thiếu / còn chữ gốc, batch nhỏ hơn
        for rnd, small in enumerate((max(1, bs // 4), 5), 1):
            src = {c.index: c.text for c in targets}
            miss = {str(i): t for i, t in src.items()
                    if needs_retranslation(t, result.get(i), self.settings.source_lang)}
            if not miss or self.stop.is_set():
                break
            self.cb.log(f"Dịch bù vòng {rnd}: {len(miss)} câu sót (batch {small})")
            got = self._run(miss, small)
            result.update({int(k): v for k, v in got.items() if v.strip()})
        return result
