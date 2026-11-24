"""Nghe giọng trong video/âm thanh -> phụ đề gốc (dùng Gemini trên Vertex AI).

Quy trình: ffmpeg tách tiếng (mono 16 kHz) -> tìm chỗ im lặng -> cắt thành đoạn ~2 phút
-> gửi song song cho Gemini chép lời kèm thời gian -> cộng bù thời gian -> ghép thành SRT.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .providers import Provider, ProviderError, RetryableError, VertexProvider
from .srt import Cue

MEDIA_EXT = {".mp4", ".mkv", ".flv", ".mov", ".avi", ".webm", ".ts", ".m4v",
             ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}

CHUNK_TARGET = 120.0   # giây mỗi đoạn
CHUNK_SLACK = 25.0     # được lệch tối đa để cắt đúng chỗ im lặng


def is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXT


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        exe = shutil.which("ffmpeg")
        if not exe:
            raise ProviderError("Không tìm thấy ffmpeg — chạy: pip install imageio-ffmpeg")
        return exe


def _run(args: list[str]) -> subprocess.CompletedProcess:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: không bật cửa sổ đen
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=flags)


def extract_audio(src: str, dst_wav: str) -> float:
    """Tách tiếng ra WAV mono 16 kHz. Trả về độ dài (giây)."""
    r = _run([ffmpeg_exe(), "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", dst_wav])
    if r.returncode != 0 or not os.path.exists(dst_wav):
        tail = "\n".join(r.stderr.strip().splitlines()[-3:])
        raise ProviderError(f"ffmpeg không tách được tiếng: {tail}")
    m = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", r.stderr)
    if m:
        h, mi, s = m[-1]
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return os.path.getsize(dst_wav) / 32000.0  # 16 kHz * 2 byte


def silences(wav: str) -> list[float]:
    """Điểm giữa các khoảng im lặng (giây)."""
    r = _run([ffmpeg_exe(), "-i", wav, "-af", "silencedetect=noise=-32dB:d=0.35", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?\d+\.?\d*)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (\d+\.?\d*)", r.stderr)]
    return [max(0.0, (s + e) / 2) for s, e in zip(starts, ends)]


def plan_chunks(duration: float, quiet: list[float],
                target: float = CHUNK_TARGET, slack: float = CHUNK_SLACK) -> list[tuple[float, float]]:
    """Chia [0, duration] thành các đoạn ~target giây, cắt ở chỗ im lặng gần nhất nếu có."""
    cuts, pos = [], 0.0
    while duration - pos > target + slack:
        want = pos + target
        near = [q for q in quiet if abs(q - want) <= slack and q > pos + 10]
        cut = min(near, key=lambda q: abs(q - want)) if near else want
        cuts.append(cut)
        pos = cut
    edges = [0.0] + cuts + [duration]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def cut_chunk(wav: str, start: float, end: float, dst: str) -> bytes:
    """Cắt một đoạn và nén MP3 32 kbps (nhẹ để gửi lên API)."""
    r = _run([ffmpeg_exe(), "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", wav,
              "-ac", "1", "-ar", "16000", "-b:a", "32k", dst])
    if r.returncode != 0:
        raise ProviderError(f"ffmpeg không cắt được đoạn {start:.0f}s: {r.stderr[-200:]}")
    return open(dst, "rb").read()


def build_prompt(lang: str, hints: str) -> str:
    p = (
        f"Đây là một đoạn âm thanh trong phim, ngôn ngữ nói: {lang}.\n"
        "Hãy CHÉP LẠI NGUYÊN VĂN lời thoại (không dịch), chia thành từng câu phụ đề ngắn "
        "(tối đa ~20 chữ Hán hoặc ~12 từ mỗi câu).\n"
        "Trả về DUY NHẤT một JSON array, mỗi phần tử: "
        '{"s": giây_bắt_đầu, "e": giây_kết_thúc, "t": "lời thoại"} — thời gian tính từ đầu đoạn âm thanh này, '
        "số thực có 1-2 chữ số thập phân, tăng dần, không chồng lên nhau.\n"
        "Bỏ qua nhạc nền, tiếng động; không thêm chú thích như [nhạc]. Chỗ không có lời thì không ghi gì. "
        "Nếu cả đoạn không có lời thoại, trả về []."
    )
    if lang == "Tiếng Trung":
        p += " Dùng chữ Hán giản thể."
    if hints.strip():
        p += "\n\nTên riêng / thuật ngữ có thể xuất hiện (viết đúng như sau):\n" + hints.strip()
    return p


def parse_segments(text: str, length: float) -> list[tuple[float, float, str]]:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    a, b = t.find("["), t.rfind("]")
    if a == -1 or b == -1:
        raise ValueError("không thấy JSON array")
    raw = json.loads(t[a:b + 1])
    segs = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        txt = str(it.get("t") or it.get("text") or "").strip()
        if not txt:
            continue
        s = _sec(it.get("s", it.get("start")))
        e = _sec(it.get("e", it.get("end")))
        if s is None or s >= length - 0.2:
            continue  # thời gian nằm ngoài đoạn -> AI đoán sai, bỏ
        s = max(0.0, s)
        e = min(max(s + 0.4, e if e is not None else s + 2), length + 0.5)
        segs.append((s, e, txt))
    segs.sort(key=lambda x: x[0])
    # không cho chồng lên nhau, câu tối thiểu 0.6s, tối đa 8s
    out = []
    for i, (s, e, txt) in enumerate(segs):
        if out and s < out[-1][1]:
            ps, pe, pt = out[-1]
            out[-1] = (ps, max(ps + 0.3, s - 0.05), pt)
        nxt = segs[i + 1][0] if i + 1 < len(segs) else length + 0.5
        e = min(max(e, s + 0.6), s + 8.0, max(s + 0.3, nxt - 0.05))
        out.append((s, e, txt))
    return out


def _sec(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip().replace(",", ".")
    try:
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


@dataclass
class Transcriber:
    providers: list[Provider]
    lang: str = "Tiếng Trung"
    hints: str = ""
    threads: int = 4
    stop: threading.Event | None = None
    log: callable = print
    progress: callable = lambda done, total: None
    event: callable = lambda e: None
    max_retries: int = 4

    def __post_init__(self):
        self.providers = [p for p in self.providers if isinstance(p, VertexProvider)]
        if not self.providers:
            raise ProviderError("Nghe giọng cần Vertex AI: bật “Dùng nhà cung cấp này khi dịch” "
                                "cho Vertex AI (API key hoặc Service account JSON)")
        self.stop = self.stop or threading.Event()
        self._rr = 0
        self._lock = threading.Lock()

    def _provider(self) -> VertexProvider:
        with self._lock:
            p = self.providers[self._rr % len(self.providers)]
            self._rr += 1
            return p

    def _job(self, idx: int, state: str, **kw):
        self.event({"type": "job", "id": f"nghe-{idx}", "state": state, **kw})

    def _one(self, idx: int, audio: bytes, length: float) -> list[tuple[float, float, str]]:
        prompt = build_prompt(self.lang, self.hints)
        last = ""
        for attempt in range(1, self.max_retries + 1):
            if self.stop.is_set():
                return []
            prov = self._provider()
            self._job(idx, "run", src=getattr(prov, "label", ""),
                      info=f"Gemini đang nghe {length:.0f}s âm thanh" + (f" (lần {attempt})" if attempt > 1 else ""))
            try:
                reply = prov.generate(
                    [{"inlineData": {"mimeType": "audio/mp3", "data": base64.b64encode(audio).decode()}},
                     {"text": prompt}],
                    json_mode=True)
                segs = parse_segments(reply, length)
                self._job(idx, "done", info=f"nghe được {len(segs)} câu")
                return segs
            except ProviderError as e:
                self._job(idx, "fail", info=str(e)[:120])
                raise
            except (RetryableError, ValueError) as e:
                last = str(e)
                wait = getattr(e, "wait", None) or min(30, 2 ** attempt)
                self._job(idx, "retry", info=f"{last[:80]} — chờ {wait:.0f}s ({attempt}/{self.max_retries})")
                self.log(f"  ⚠ Đoạn {idx + 1}: {last[:120]} → thử lại sau {wait:.0f}s ({attempt}/{self.max_retries})")
                if self.stop.wait(wait):
                    return []
        self._job(idx, "fail", info=f"bỏ qua sau {self.max_retries} lần")
        self.log(f"  ✖ Đoạn {idx + 1}: bỏ qua sau {self.max_retries} lần ({last[:100]})")
        return []

    def run(self, media: str) -> list[Cue]:
        tmp = tempfile.mkdtemp(prefix="st_audio_")
        try:
            wav = os.path.join(tmp, "audio.wav")
            self.log("  Đang tách tiếng từ video…")
            self.event({"type": "stage", "stage": "audio", "text": "Đang tách tiếng từ video bằng ffmpeg"})
            dur = extract_audio(media, wav)
            self.event({"type": "stage", "stage": "audio", "text": "Đang tìm chỗ im lặng để cắt đoạn"})
            chunks = plan_chunks(dur, silences(wav))
            self.log(f"  Âm thanh dài {int(dur // 60)}:{int(dur % 60):02d} → {len(chunks)} đoạn, "
                     f"{min(self.threads, len(chunks))} luồng nghe")
            data = [cut_chunk(wav, s, e, os.path.join(tmp, f"c{i}.mp3")) for i, (s, e) in enumerate(chunks)]
            for i, (s, e) in enumerate(chunks):
                self._job(i, "queued", name=f"Nghe đoạn {i + 1}/{len(chunks)} · {_mmss(s)}–{_mmss(e)}",
                          info="đang chờ luồng trống")
            self.event({"type": "stage", "stage": "listen",
                        "text": f"Đang nghe giọng: {len(chunks)} đoạn, {_mmss(dur)} âm thanh"})
            results: dict[int, list] = {}
            done = 0
            self.progress(0, len(chunks))
            with ThreadPoolExecutor(max_workers=max(1, self.threads)) as ex:
                futs = {ex.submit(self._one, i, data[i], e - s): i for i, (s, e) in enumerate(chunks)}
                for f in as_completed(futs):
                    results[futs[f]] = f.result()
                    done += 1
                    self.progress(done, len(chunks))
            return merge_chunks(chunks, results)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def merge_chunks(chunks: list[tuple[float, float]], results: dict[int, list]) -> list[Cue]:
    """Ghép kết quả các đoạn thành phụ đề; sửa chồng thời gian ở chỗ nối giữa hai đoạn."""
    segs = []
    for i, (start, end) in enumerate(chunks):
        for s, e, txt in results.get(i, []):
            segs.append([start + s, min(start + e, end + 0.5), txt])
    segs.sort(key=lambda x: x[0])
    for a, b in zip(segs, segs[1:]):
        if b[0] < a[1]:
            a[1] = max(a[0] + 0.3, b[0] - 0.05)
    return [Cue(i + 1, _ts(s), _ts(e), txt) for i, (s, e, txt) in enumerate(segs)]



def _mmss(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
