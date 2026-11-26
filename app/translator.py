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
    dub_durations: dict = None  # {id: giây} -> dịch kiểu lồng tiếng (vừa thời lượng nói)
    glossary: dict = None       # {thuật ngữ gốc: cách dịch bắt buộc} dùng chung cả bộ phim
    refine: bool = False        # dịch kỹ: thêm lượt biên tập (rà lại + viết lại tự nhiên)


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
    if s.dub_durations:
        base += ("\n\nBẢN DỊCH DÙNG ĐỂ LỒNG TIẾNG (đọc thành giọng nói):\n"
                 "- Mỗi câu gốc có ghi chú thời lượng dạng ⟪2.4s⟫ ở cuối: câu dịch phải ĐỌC KỊP trong thời gian đó "
                 "(khoảng 4–5 âm tiết tiếng Việt mỗi giây). Câu ngắn thì dịch ngắn, rút gọn ý phụ, bỏ từ thừa.\n"
                 "- Văn nói tự nhiên, dễ đọc thành tiếng; không dùng ký hiệu, ngoặc, chú thích.\n"
                 "- KHÔNG đưa ghi chú ⟪…s⟫ vào bản dịch.")
    if s.custom_prompt.strip():
        base += "\n\nHướng dẫn riêng cho phim này (ưu tiên làm theo):\n" + s.custom_prompt.strip()
    return base


def parse_json_reply(text: str) -> dict[str, str]:
    from .providers import strip_thinking
    t = strip_thinking(text)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("không thấy JSON")
    obj = json.loads(t[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON không phải object")
    return {str(k).strip(): ("" if v is None else str(v)) for k, v in obj.items()}


# ---------------------------------------------------------------- thuật ngữ
def glossary_for(text: str, glossary: dict | None) -> dict[str, str]:
    """Các thuật ngữ thật sự xuất hiện trong đoạn này (để không gửi cả bảng mỗi lần)."""
    if not glossary:
        return {}
    return {k: v for k, v in glossary.items() if k and v and k in text}


def glossary_miss(src: str, dst: str | None, glossary: dict | None) -> list[str]:
    """Thuật ngữ có trong câu gốc nhưng bản dịch không dùng đúng cách dịch đã chốt."""
    if not glossary or not dst:
        return []
    low = dst.lower()
    return [k for k, v in glossary_for(src, glossary).items() if v.lower() not in low]


def glossary_block(terms: dict[str, str]) -> str:
    if not terms:
        return ""
    return ("THUẬT NGỮ BẮT BUỘC (dùng đúng cách dịch này, kể cả khi có thể dịch khác):\n"
            + "\n".join(f"- {k} = {v}" for k, v in terms.items()) + "\n\n")


REFINE_PROMPT = (
    "Bạn là biên tập viên phụ đề {tgt}. Mỗi mục có câu gốc ({src}) và bản dịch nháp.\n"
    "Hãy rà lại và viết lại bản dịch: sửa chỗ dịch sai ý, sai xưng hô, sai thuật ngữ; viết lại cho tự nhiên như "
    "người {tgt} nói, ngắn gọn, đúng giọng nhân vật. Giữ nguyên các thẻ <i>, {{\\an8}}, \\n nếu có.\n"
    "Nếu bản nháp đã tốt thì giữ nguyên. Không thêm giải thích.\n"
    "Đầu vào: JSON {{\"id\": {{\"goc\": \"...\", \"nhap\": \"...\"}}}}. "
    "Chỉ trả về JSON {{\"id\": \"bản dịch cuối\"}} có ĐÚNG các id đó, không markdown."
)


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
    # sự kiện chi tiết cho giao diện:
    #  {"type": "stage", "stage": "translate"|"fix"|..., "text": "..."}
    #  {"type": "job", "id": "...", "name": "...", "state": "queued|run|retry|done|fail",
    #   "src": "nguồn đang gửi", "info": "..."}
    event: Callable[[dict], None] = lambda e: None


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
        self.refine_system = REFINE_PROMPT.format(src=self.settings.source_lang, tgt=self.settings.target_lang)
        if self.settings.custom_prompt.strip():
            self.refine_system += "\n\nHướng dẫn riêng cho phim này:\n" + self.settings.custom_prompt.strip()
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
    def _job(self, jid: str, state: str, **kw):
        self.cb.event({"type": "job", "id": jid, "state": state, **kw})

    def _translate_batch(self, items: dict[str, str], jid: str = "") -> dict[str, str]:
        durs = self.settings.dub_durations or {}
        send = {k: (f"{v} ⟪{float(durs[k]):.1f}s⟫" if k in durs else v) for k, v in items.items()}
        terms = glossary_for("\n".join(items.values()), self.settings.glossary)
        user = glossary_block(terms) + json.dumps(send, ensure_ascii=False)
        got = self._ask(self.system, user, items, jid, "dịch")
        if got and self.settings.refine and not self.stop.is_set():
            draft = {k: {"goc": items[k], "nhap": got[k]} for k in got}
            if durs:
                for k in draft:
                    if k in durs:
                        draft[k]["thoi_luong"] = f"{float(durs[k]):.1f}s (đọc kịp trong thời gian này)"
            user2 = glossary_block(terms) + json.dumps(draft, ensure_ascii=False)
            better = self._ask(self.refine_system, user2, got, jid, "biên tập")
            got.update({k: v for k, v in better.items() if v.strip()})
        return got

    def _ask(self, system: str, user: str, items: dict[str, str], jid: str, what: str) -> dict[str, str]:
        last_err = None
        for attempt in range(1, self.settings.max_retries + 1):
            if self.stop.is_set():
                raise Stopped()
            if self.limiter.interval:
                self._job(jid, "queued", info="chờ lượt theo giới hạn request/phút")
            self.limiter.wait(self.stop)
            prov = self._next_provider()
            self._job(jid, "run", src=getattr(prov, "label", ""),
                      info=f"đang {what} {len(items)} câu" + (f" (lần {attempt})" if attempt > 1 else ""))
            try:
                reply = prov.chat(system, user)
                result = parse_json_reply(reply)
                got = {k: re.sub(r"\s*⟪[^⟫]*⟫\s*", " ", result[k]).strip() for k in items if k in result}
                if len(got) >= max(1, int(len(items) * 0.5)):
                    self._job(jid, "done", info=f"{what} xong {len(got)}/{len(items)} câu")
                    return got  # thiếu vài câu thì để vòng dịch bù xử lý
                last_err = f"chỉ nhận {len(got)}/{len(items)} câu"
            except ProviderError as e:
                if self._drop(prov, e):
                    self._job(jid, "retry", info="nguồn lỗi, chuyển sang nguồn khác")
                    continue  # thử lại batch này bằng nguồn khác
                self._job(jid, "fail", info=str(e)[:120])
                raise
            except RetryableError as e:
                last_err = str(e)
                wait = e.wait or min(60, 2 ** attempt + random.random())
                self._job(jid, "retry", info=f"{last_err[:80]} — chờ {wait:.0f}s ({attempt}/{self.settings.max_retries})")
                self.cb.log(f"  ⚠ {last_err[:150]} → chờ {wait:.0f}s thử lại ({attempt}/{self.settings.max_retries})")
                if self.stop.wait(wait):
                    raise Stopped()
                continue
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"JSON lỗi: {e}"
            self._job(jid, "retry", info=f"{last_err[:80]} ({attempt}/{self.settings.max_retries})")
            self.cb.log(f"  ⚠ {last_err} → thử lại ({attempt}/{self.settings.max_retries})")
            if self.stop.wait(min(20, attempt * 2)):
                raise Stopped()
        self._job(jid, "fail", info=f"bỏ qua sau {self.settings.max_retries} lần: {last_err}"[:120])
        self.cb.log(f"  ✖ Bỏ qua batch sau {self.settings.max_retries} lần: {last_err}")
        return {}

    # ---- chạy nhiều batch song song ----
    def _run(self, items: dict[str, str], batch_size: int, done_offset=0, total=None,
             phase: str = "Batch") -> dict[str, str]:
        keys = list(items)
        batches = [
            {k: items[k] for k in keys[i : i + batch_size]} for i in range(0, len(keys), batch_size)
        ]
        jids = []
        for i, b in enumerate(batches, 1):
            ks = list(b)
            nums = [int(k) for k in ks if k.isdigit()]
            if len(ks) == 1:
                rng = f"câu {ks[0]}"
            elif nums and nums == list(range(nums[0], nums[0] + len(nums))):
                rng = f"câu {ks[0]}–{ks[-1]}"
            else:  # câu lẻ (dịch bù): liệt kê
                rng = f"{len(ks)} câu: " + ", ".join(ks[:4]) + ("…" if len(ks) > 4 else "")
            jid = f"{phase}-{i}-{id(b)}"
            jids.append(jid)
            disp = "Batch bù" if phase.startswith("Bù") else "Batch"
            self._job(jid, "queued", name=f"{disp} {i}/{len(batches)} · {rng}", info="đang chờ luồng trống")
        total = total or len(batches)
        out: dict[str, str] = {}
        done = done_offset
        self.cb.progress(done, total)
        with ThreadPoolExecutor(max_workers=max(1, self.settings.threads)) as ex:
            futs = [ex.submit(self._translate_batch, b, jids[i]) for i, b in enumerate(batches)]
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
        self.cb.event({"type": "stage", "stage": "translate",
                       "text": f"Đang dịch {len(items)} câu ({n_batches} batch)"})
        got = self._run(items, bs, phase="Dịch")
        result.update({int(k): v for k, v in got.items()})

        # Vòng dịch bù: câu thiếu / còn chữ gốc, batch nhỏ hơn
        for rnd, small in enumerate((max(1, bs // 4), 5), 1):
            src = {c.index: c.text for c in targets}
            miss = {str(i): t for i, t in src.items()
                    if needs_retranslation(t, result.get(i), self.settings.source_lang)
                    or glossary_miss(t, result.get(i), self.settings.glossary)}
            if not miss or self.stop.is_set():
                break
            self.cb.log(f"Dịch bù vòng {rnd}: {len(miss)} câu sót (batch {small})")
            self.cb.event({"type": "stage", "stage": "fix",
                           "text": f"Dịch bù vòng {rnd}: {len(miss)} câu bị sót hoặc còn chữ gốc"})
            got = self._run(miss, small, phase=f"Bù {rnd}")
            result.update({int(k): v for k, v in got.items() if v.strip()})
        return result


# ---------------------------------------------------------------- lập bảng thuật ngữ bằng AI
GLOSSARY_PROMPT = (
    "Bạn là biên tập viên dịch phim {src} sang {tgt}. Đọc phụ đề gốc dưới đây và liệt kê các THUẬT NGỮ cần dịch "
    "thống nhất suốt bộ phim: tên nhân vật, danh xưng/xưng hô đặc biệt (sư tôn, sư huynh…), địa danh, môn phái, "
    "cảnh giới tu luyện, chiêu thức, bảo vật, tổ chức. Với phim cổ trang/tiên hiệp dùng âm Hán Việt cho tên riêng.\n"
    "Chỉ lấy từ thật sự xuất hiện trong phụ đề, không bịa. Bỏ qua từ thông thường.\n"
    "Trả về JSON: {{\"terms\": [{{\"src\": \"nguyên văn\", \"vi\": \"cách dịch\", \"loai\": \"nhân vật|địa danh|chiêu thức|…\"}}]}}. "
    "Không markdown, không giải thích."
)


def extract_glossary(providers, texts: list[str], source_lang: str, target_lang: str = "Tiếng Việt",
                     log=print, stop: threading.Event | None = None, chunk: int = 400) -> list[dict]:
    """Gửi phụ đề gốc theo từng phần, gom các thuật ngữ AI đề xuất (bỏ trùng, chỉ giữ từ có trong phụ đề)."""
    from .providers import ProviderError, RetryableError, strip_thinking
    providers = providers if isinstance(providers, list) else [providers]
    system = GLOSSARY_PROMPT.format(src=source_lang, tgt=target_lang)
    lines = [t.replace("\n", " ").strip() for t in texts if t and t.strip()]
    found: dict[str, dict] = {}
    for i in range(0, len(lines), chunk):
        if stop is not None and stop.is_set():
            break
        part = "\n".join(lines[i:i + chunk])
        last = None
        for attempt in range(4):
            prov = providers[(i // chunk + attempt) % len(providers)]
            try:
                raw = strip_thinking(prov.chat(system, part))
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
                obj = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
                for t in obj.get("terms", []):
                    src, vi = str(t.get("src", "")).strip(), str(t.get("vi", "")).strip()
                    if src and vi and src in part and src not in found:
                        found[src] = {"src": src, "vi": vi, "loai": str(t.get("loai", "")).strip()}
                log(f"  Thuật ngữ: phần {i // chunk + 1}, đã có {len(found)} mục")
                break
            except (ProviderError, RetryableError, ValueError, json.JSONDecodeError) as e:
                last = e
                time.sleep(min(10, 2 ** attempt))
        else:
            log(f"  ⚠ Bỏ qua một phần khi lập thuật ngữ: {last}")
    # tên dài trước (林澈 trước 林) để khi thay không bị cắt
    return sorted(found.values(), key=lambda d: -len(d["src"]))


def load_glossary(path: str) -> dict[str, str]:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {d["src"]: d["vi"] for d in data.get("terms", []) if d.get("src") and d.get("vi")}
