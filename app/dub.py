"""Lồng tiếng Việt: nhận diện nhân vật, đọc từng câu bằng VieNeu-TTS, khớp thời gian, trộn với nhạc nền.

Quy trình (mỗi bước lưu file trung gian trong thư mục <tên video>_long_tieng/):
  1. tách giọng (vocal.py)            -> instrumental.wav, vocals.wav
  2. nghe giọng (transcribe.py)        -> <tên>.zh.srt + speakers.json (giới tính/người nói/cảm xúc)
  3. dịch kiểu lồng tiếng (translator) -> <tên>.vi.srt
  4. nhận diện nhân vật + gán giọng    -> project.json
  5. đọc từng câu (VieNeu)             -> tts/<hash>.wav
  6. trộn                              -> <tên>.vi.dub.m4a
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import srt
from .vocal import Stopped, load_audio, save_wav  # Stopped dùng chung: dừng khi tách giọng cũng nhận ra

SR = 48000


# ====================================================================== dữ liệu dự án
@dataclass
class Character:
    name: str
    gender: str = ""        # "nam" | "nữ" | ""
    desc: str = ""          # vd: "sư tôn, giọng trầm"
    voice: str = ""         # id giọng VieNeu
    speed: float = 1.0      # tốc độ đọc riêng của nhân vật (như thanh Tốc độ của CapCut)
    pitch: float = 0.0      # cao độ, nửa cung (-8 trầm … +8 cao), không đổi tốc độ
    volume: float = 1.0     # âm lượng riêng


@dataclass
class Line:
    index: int
    start: float
    end: float
    text: str               # lời Việt sẽ đọc
    src: str = ""           # lời gốc
    speaker: str = ""
    emotion: str = ""
    gender_hint: str = ""   # giới tính nghe được từ âm thanh


@dataclass
class Project:
    video: str
    workdir: str
    characters: list[Character] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)

    @property
    def path(self) -> str:
        return os.path.join(self.workdir, "project.json")

    def save(self):
        os.makedirs(self.workdir, exist_ok=True)
        data = {"video": self.video, "video_speed": getattr(self, "video_speed", 1.0), "characters": [asdict(c) for c in self.characters],
                "lines": [asdict(l) for l in self.lines]}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @classmethod
    def load(cls, video: str, workdir: str) -> "Project":
        p = cls(video, workdir)
        if os.path.exists(p.path):
            d = json.load(open(p.path, encoding="utf-8"))
            cf = set(Character.__dataclass_fields__); lf = set(Line.__dataclass_fields__)
            p.characters = [Character(**{k: v for k, v in c.items() if k in cf}) for c in d.get("characters", [])]
            p.lines = [Line(**{k: v for k, v in l.items() if k in lf}) for l in d.get("lines", [])]
            p.video_speed = float(d.get("video_speed", 1.0))
        return p

    def char(self, name: str) -> Character | None:
        return next((c for c in self.characters if c.name == name), None)


def workdir_for(video: str, out_dir: str = "") -> str:
    p = Path(video)
    return str((Path(out_dir) if out_dir else p.parent) / f"{p.stem}_long_tieng")


def ts_seconds(t: str) -> float:
    hh, mm, rest = t.replace(".", ",").split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


def lines_from_srt(vi_srt: str, zh_srt: str | None = None, speakers: dict | None = None) -> list[Line]:
    vi = srt.parse(srt.read_text(vi_srt))
    zh = srt.parse(srt.read_text(zh_srt)) if zh_srt and os.path.exists(zh_srt) else []
    speakers = speakers or {}
    out = []
    for i, c in enumerate(vi):
        src = zh[i].text if i < len(zh) else ""
        meta = speakers.get(str(c.index), {})
        out.append(Line(c.index, ts_seconds(c.start), ts_seconds(c.end), c.text.replace("\n", " ").strip(), src,
                        emotion=meta.get("emo", ""), gender_hint=meta.get("g", ""),
                        speaker=meta.get("spk", "")))
    return out


# ====================================================================== nhận diện nhân vật
def _json_from(text: str):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    a, b = t.find("{"), t.rfind("}")
    return json.loads(t[a:b + 1])


def analyze_characters(provider, lines: list[Line], log=print, chunk: int = 120,
                       stop: threading.Event | None = None) -> tuple[list[Character], dict[int, tuple[str, str]]]:
    """Gemini đọc kịch bản (gốc + Việt + gợi ý giới tính nghe được) -> danh sách nhân vật và người nói mỗi câu."""
    chars: dict[str, Character] = {}
    assign: dict[int, tuple[str, str]] = {}
    system = (
        "Bạn là biên tập viên lồng tiếng phim. Từ kịch bản phụ đề, hãy xác định câu nào do NHÂN VẬT nào nói.\n"
        "- Dùng tên nhân vật bằng tiếng Việt (Hán Việt nếu là phim Trung). Nếu không biết tên, đặt nhãn ngắn như "
        "'Người dẫn chuyện', 'Nam lạ 1', 'Nữ tỳ'.\n"
        "- Lời kể/thuyết minh (không phải hội thoại) gán cho 'Người dẫn chuyện'.\n"
        "- Gợi ý [g=nam/nữ] là giới tính nghe được từ âm thanh, hãy tin gợi ý này.\n"
        "- emotion: 1-3 từ (bình thường, giận dữ, buồn, sợ hãi, vui, thì thầm, quát, đau đớn...).\n"
        "Chỉ trả về JSON: {\"characters\": [{\"name\": \"...\", \"gender\": \"nam|nữ\", \"desc\": \"vai trò, tuổi, chất giọng\"}], "
        "\"lines\": {\"<id>\": {\"speaker\": \"...\", \"emotion\": \"...\"}}}"
    )
    for i in range(0, len(lines), chunk):
        if stop is not None and stop.is_set():
            raise Stopped()
        part = lines[i:i + chunk]
        known = "\n".join(f"- {c.name} ({c.gender}): {c.desc}" for c in chars.values()) or "(chưa có)"
        script = "\n".join(
            f"{l.index}. {('[g=' + l.gender_hint + '] ') if l.gender_hint else ''}{l.src or ''} | {l.text}" for l in part)
        user = f"Nhân vật đã biết:\n{known}\n\nKịch bản (id. [gợi ý] lời gốc | lời Việt):\n{script}"
        log(f"  Nhận diện nhân vật: câu {part[0].index}–{part[-1].index}")
        data = None
        for attempt in range(3):
            try:
                data = _json_from(provider.chat(system, user))
                break
            except Exception as e:  # noqa: BLE001
                log(f"  ⚠ thử lại ({attempt + 1}/3): {str(e)[:120]}")
        if not data:
            continue
        for c in data.get("characters") or []:
            name = str(c.get("name") or "").strip()
            if name and name not in chars:
                g = str(c.get("gender") or "").lower()
                chars[name] = Character(name, "nữ" if g.startswith("n") and "ữ" in g or g in ("nu", "female", "f") else
                                        ("nam" if g else ""), str(c.get("desc") or ""))
        for k, v in (data.get("lines") or {}).items():
            if str(k).isdigit() and isinstance(v, dict):
                assign[int(k)] = (str(v.get("speaker") or "").strip(), str(v.get("emotion") or "").strip())
    for sp, _ in assign.values():
        if sp and sp not in chars:
            chars[sp] = Character(sp)
    return list(chars.values()), assign


# ====================================================================== giọng đọc
class TTSEngine:
    """Bọc VieNeu-TTS (v3 Turbo: card NVIDIA nếu có PyTorch CUDA, không thì ONNX/CPU)."""

    def __init__(self, log=print):
        self.log = log
        self.tts = None
        self.sr = SR
        self.device = ""
        self._lock = threading.Lock()

    def load(self):
        if self.tts is not None:
            return
        try:
            from vieneu import Vieneu
        except ImportError as e:
            raise RuntimeError("Chưa cài VieNeu-TTS: chạy  pip install vieneu  (card NVIDIA: chạy cai_long_tieng_gpu.bat)") from e
        self.log("  Đang nạp VieNeu-TTS (lần đầu sẽ tải model từ Hugging Face, vài trăm MB)…")
        self.tts = Vieneu()
        self.sr = int(getattr(self.tts, "sample_rate", SR))
        try:
            import torch
            self.device = "GPU " + torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        except Exception:  # noqa: BLE001
            self.device = "CPU"
        self.log(f"  VieNeu sẵn sàng ({self.device}, {self.sr} Hz)")

    def voices(self) -> list[tuple[str, str]]:
        self.load()
        return list(self.tts.list_preset_voices())

    def synth(self, text: str, voice: str) -> np.ndarray:
        self.load()
        with self._lock:
            wav = self.tts.infer(text, voice=voice or None)
        return np.asarray(wav, dtype=np.float32).reshape(-1)


def voice_gender(label: str) -> str:
    """Đoán giới tính giọng từ nhãn của VieNeu (vd: '⭐ Hải Đăng (Nam miền Bắc)')."""
    s = label.lower()
    if re.search(r"\bnữ\b|\(nữ|female", s):
        return "nữ"
    if re.search(r"\(nam\b|giọng nam|\bmale\b|nam miền|nam giọng", s):
        return "nam"
    return ""


def auto_assign_voices(chars: list[Character], voices: list[tuple[str, str]]):
    """Gán giọng khác nhau cho từng nhân vật theo giới tính; giữ giọng đã chọn."""
    by_g = {"nam": [v for l, v in voices if voice_gender(l) == "nam"],
            "nữ": [v for l, v in voices if voice_gender(l) == "nữ"]}
    allv = [v for _, v in voices]
    used = {c.voice for c in chars if c.voice}
    ordered = sorted(chars, key=lambda c: (c.name != "Người dẫn chuyện"))
    for c in ordered:
        if c.voice:
            continue
        pool = by_g.get(c.gender) or allv
        free = [v for v in pool if v not in used] or pool or allv
        if free:
            c.voice = free[0]
            used.add(c.voice)


def emotion_text(text: str, emotion: str) -> str:
    """VieNeu v3 hiểu vài thẻ cảm xúc thử nghiệm: [cười], [thở dài]."""
    e = (emotion or "").lower()
    if "cười" in e:
        return "[cười] " + text
    if "thở dài" in e:
        return "[thở dài] " + text
    return text


# ====================================================================== khớp thời gian + trộn
def _ffmpeg():
    from .transcribe import ffmpeg_exe
    return ffmpeg_exe()


def _flags():
    return 0x08000000 if sys.platform == "win32" else 0


def _atempo(f: float) -> list[str]:
    chain = []
    while f > 2.0:
        chain.append("atempo=2.0"); f /= 2.0
    while f < 0.5:
        chain.append("atempo=0.5"); f /= 0.5
    chain.append(f"atempo={f:.4f}")
    return chain


def tempo(wav: np.ndarray, factor: float, sr: int = SR, pitch: float = 0.0) -> np.ndarray:
    """Đọc nhanh hơn `factor` lần, giữ cao độ (atempo); pitch: lên/xuống nửa cung mà không đổi tốc độ."""
    if abs(factor - 1) < 0.02 and abs(pitch) < 0.05:
        return wav
    chain = []
    if abs(pitch) >= 0.05:
        r = 2 ** (pitch / 12)
        chain += [f"asetrate={sr * r:.1f}", f"aresample={sr}"]
        factor = factor / r
    if abs(factor - 1) >= 0.005:
        chain += _atempo(factor)
    r = subprocess.run([_ffmpeg(), "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                        "-af", ",".join(chain), "-f", "f32le", "-"], input=wav.astype(np.float32).tobytes(),
                       capture_output=True, creationflags=_flags())
    if r.returncode != 0:
        return wav
    return np.frombuffer(r.stdout, dtype=np.float32)


def trim_silence(wav: np.ndarray, sr: int = SR, thr: float = 0.01) -> np.ndarray:
    idx = np.where(np.abs(wav) > thr)[0]
    if not len(idx):
        return wav
    a = max(0, idx[0] - int(0.03 * sr)); b = min(len(wav), idx[-1] + int(0.08 * sr))
    return wav[a:b]


def normalize(wav: np.ndarray, target_db: float = -18.0) -> np.ndarray:
    rms = float(np.sqrt(np.mean(wav ** 2)) + 1e-9)
    g = 10 ** (target_db / 20) / rms
    return np.clip(wav * min(g, 8.0), -1, 1)


@dataclass
class MixOptions:
    remove_original: bool = True     # True: dùng nhạc nền đã tách; False: tiếng gốc vặn nhỏ khi có thoại
    duck: float = 0.25               # mức tiếng gốc khi đang có thoại (0..1)
    voice_gain: float = 1.0
    bg_gain: float = 1.0
    max_speed: float = 1.35
    video_speed: float = 1.0         # tốc độ cả video (hình + nhạc nền), như Tốc độ của CapCut


def fit_lines(lines: list[Line], wavs: dict[int, np.ndarray], max_speed: float, sr: int = SR,
              log=print, max_push: float = 0.6, gap: float = 0.06) -> tuple[dict[int, np.ndarray], dict[int, float]]:
    """Khớp từng câu vào thời gian của nó, không để hai câu chồng lên nhau.

    1) câu dài hơn chỗ trống -> đọc nhanh lên (tối đa max_speed)
    2) vẫn dài -> câu sau được lùi lại tối đa max_push giây so với bản gốc
    3) vẫn dài -> cắt đuôi câu kèm mờ dần 120 ms (ghi log để sửa câu ngắn lại)
    Trả (wav đã khớp, giây bắt đầu thực tế của từng câu)."""
    order = [l for l in sorted(lines, key=lambda x: x.start) if wavs.get(l.index) is not None and len(wavs[l.index])]
    out, starts = {}, {}
    sped, pushed, cut = 0, 0, []
    prev_end = -1.0
    for i, l in enumerate(order):
        w = trim_silence(wavs[l.index], sr)
        st = max(l.start, prev_end + gap)
        if st > l.start + 0.01:
            pushed += 1
        nxt = order[i + 1].start + max_push if i + 1 < len(order) else st + (len(w) / sr) + 10
        slot = max(0.3, max(l.end, nxt - max_push) - st)   # khoảng lý tưởng: tới đầu câu sau
        limit = max(0.3, nxt - st - gap)                  # khoảng tối đa: tới câu sau đã lùi hết mức
        dur = len(w) / sr
        if dur > slot:
            f = min(dur / slot, max_speed)
            if f > 1.02:
                w = tempo(w, f, sr); sped += 1
        if len(w) / sr > limit:
            n = int(limit * sr)
            k = min(int(0.12 * sr), n)
            w = w[:n].copy()
            if k > 1:
                w[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
            cut.append(l.index)
        out[l.index] = w
        starts[l.index] = st
        prev_end = st + len(w) / sr
    if pushed:
        log(f"  {pushed} câu được lùi lại một chút (≤{max_push:.1f}s) để không chồng lên câu trước")
    if cut:
        log(f"  ⚠ {len(cut)} câu quá dài dù đã đọc nhanh {max_speed}x nên bị cắt đuôi: "
            + ", ".join(f"#{i}" for i in cut[:15]) + (" …" if len(cut) > 15 else "")
            + ". Hãy sửa các câu này ngắn lại ở bảng Câu thoại rồi bấm “Tạo lại câu đã chọn”.")
    return out, starts


def mix(lines: list[Line], voice: dict[int, np.ndarray], bg: np.ndarray, opt: MixOptions, sr: int = SR,
        gains: dict[int, float] | None = None, starts: dict[int, float] | None = None) -> np.ndarray:
    """bg [2, n] -> [2, n] đã trộn giọng Việt."""
    n = bg.shape[1]
    track = np.zeros(n, np.float32)
    active = np.zeros(n, np.float32)
    for l in lines:
        w = voice.get(l.index)
        if w is None:
            continue
        a = int((starts or {}).get(l.index, l.start) * sr)
        if a >= n:
            continue
        b = min(n, a + len(w))
        track[a:b] += normalize(w[:b - a]) * opt.voice_gain * (gains or {}).get(l.index, 1.0)
        active[a:b] = 1.0
    out = bg * opt.bg_gain
    if not opt.remove_original:
        # vặn nhỏ tiếng gốc khi có thoại, chuyển mượt 120 ms
        k = int(0.12 * sr)
        if k > 1:
            env = np.convolve(active, np.ones(k, np.float32) / k, mode="same")
        else:
            env = active
        gain = 1.0 - (1.0 - opt.duck) * np.clip(env, 0, 1)
        out = out * gain[None, :]
    out = out + track[None, :]
    peak = float(np.abs(out).max() or 1.0)
    if peak > 0.99:
        out = out / peak * 0.99
    return out.astype(np.float32)


def encode_m4a(wav_path: str, out: str):
    r = subprocess.run([_ffmpeg(), "-v", "error", "-y", "-i", wav_path, "-c:a", "aac", "-b:a", "192k", out],
                       capture_output=True, creationflags=_flags())
    if r.returncode != 0:
        raise RuntimeError("ffmpeg không nén được âm thanh: " + r.stderr.decode("utf-8", "replace")[-300:])


def speed_bg(bg: np.ndarray, speed: float, sr: int = SR) -> np.ndarray:
    """Tăng/giảm tốc nhạc nền [ch, n] theo tốc độ video, giữ cao độ."""
    if abs(speed - 1) < 0.005:
        return bg
    chans = [tempo(bg[c], speed, sr) for c in range(bg.shape[0])]
    n = min(len(c) for c in chans)
    return np.stack([c[:n] for c in chans]).astype(np.float32)


def scale_lines(lines: list[Line], speed: float) -> list[Line]:
    if abs(speed - 1) < 0.005:
        return lines
    from dataclasses import replace
    return [replace(l, start=l.start / speed, end=l.end / speed) for l in lines]


def _run_stoppable(args: list[str], stop: threading.Event | None) -> tuple[int, str]:
    """Chạy ffmpeg; dừng được ngay khi stop được bật (ffmpeg nhận 'q' rồi mới bị giết)."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=_flags())
    err: list[bytes] = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    t.start()
    while proc.poll() is None:
        if stop is not None and stop.wait(0.2):
            try:
                proc.stdin.write(b"q"); proc.stdin.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise Stopped()
        elif stop is None:
            proc.wait()
    t.join(2)
    return proc.returncode, b"".join(err).decode("utf-8", "replace")


def mux_video(video: str, audio: str, out: str, speed: float = 1.0, stop: threading.Event | None = None):
    """Thay tiếng của video bằng file lồng tiếng. speed != 1: đổi tốc độ hình (phải nén lại hình)."""
    base = [_ffmpeg(), "-v", "error", "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    tail = ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out]
    if abs(speed - 1) < 0.005:
        tries = [["-c:v", "copy"]]
    else:
        vf = ["-filter:v", f"setpts=PTS/{speed:.4f}"]
        tries = [vf + ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-pix_fmt", "yuv420p"],
                 vf + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]]
    code, err = 1, ""
    for t in tries:
        code, err = _run_stoppable(base + t + tail, stop)
        if code == 0:
            return
    if code != 0:
        raise RuntimeError("ffmpeg ghép video lỗi: " + err[-300:])


# ====================================================================== đọc tất cả câu (có cache)
def line_key(text: str, voice: str) -> str:
    return hashlib.sha1(f"v3|{voice}|{text}".encode("utf-8")).hexdigest()[:20]


def synth_lines(engine: TTSEngine, proj: Project, cache_dir: str, log=print,
                progress=lambda d, t: None, stop: threading.Event | None = None,
                only: set[int] | None = None) -> dict[int, np.ndarray]:
    os.makedirs(cache_dir, exist_ok=True)
    out: dict[int, np.ndarray] = {}
    todo = [l for l in proj.lines if l.text.strip() and (only is None or l.index in only)]
    for k, l in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            raise Stopped()
        ch = proj.char(l.speaker)
        voice = ch.voice if ch else ""
        text = emotion_text(l.text, l.emotion)
        path = os.path.join(cache_dir, line_key(text, voice) + ".wav")
        if os.path.exists(path):
            w = load_audio(path, sr=engine.sr if engine.tts else SR, channels=1)[0]
        else:
            w = engine.synth(text, voice)
            save_wav(path, w, engine.sr)
        if ch and (abs(ch.speed - 1) > 0.01 or abs(ch.pitch) > 0.05):
            w = tempo(w, ch.speed, engine.sr, ch.pitch)
        out[l.index] = w
        progress(k, len(todo))
    return out


def line_cache_path(proj: Project, l: Line, cache_dir: str) -> str:
    ch = proj.char(l.speaker)
    return os.path.join(cache_dir, line_key(emotion_text(l.text, l.emotion), ch.voice if ch else "") + ".wav")

