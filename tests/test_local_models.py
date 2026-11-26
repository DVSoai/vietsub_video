"""Kiểm thử phần chạy model trên máy: Qwen3-ASR (gom chữ thành câu, cắt cửa sổ 4 phút, gắn vào Transcriber)
và BS-RoFormer (tách thật nếu đã cài audio-separator + torch; dừng giữa chừng).

Chạy: python tests/test_local_models.py
- Qwen3-ASR: môi trường kiểm thử không tải được model từ Hugging Face -> giả lập lớp Qwen3ASRModel
  (cùng giao diện: transcribe(audio=[(np, sr)], language=[...], return_time_stamps=True)).
- BS-RoFormer: chạy model thật (tải ~640 MB từ GitHub lần đầu) nếu thư viện có sẵn, không thì báo SKIP.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
QA = Path(__file__).resolve().parent / "qa_data"
QA.mkdir(exist_ok=True)

import app.hf_local as hl  # noqa: E402
from app.transcribe import Transcriber, ffmpeg_exe  # noqa: E402

FF = ffmpeg_exe()
RES: list[bool] = []


def rec(tid, name, ok, detail):
    RES.append(bool(ok) if ok is not None else True)
    tag = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"[{tag}] {tid} {name}: {detail}", flush=True)


# ---------------------------------------------------------------- Q1 gom chữ thành câu
full = "师尊，弟子来迟了。你可知罪？弟子知罪。"
chars = [c for c in full if c not in "，。？"]
items, t = [], 0.5
for i, c in enumerate(chars):
    if i == 7:
        t += 1.2                        # khoảng lặng dài trước câu 2 (“你可知罪”)
    items.append((c, t, t + 0.2)); t += 0.22
segs = hl.group_words(items, full)
texts = [s[2] for s in segs]
ok = texts == ["师尊，弟子来迟了。", "你可知罪？", "弟子知罪。"] and abs(segs[0][0] - 0.5) < 1e-6 \
    and all(b[0] >= a[1] for a, b in zip(segs, segs[1:]))
rec("Q1", "Gom chữ có giờ thành câu, giữ dấu câu, ngắt ở 。？ và khoảng lặng", ok,
    [(round(s, 2), round(e, 2), x) for s, e, x, _ in segs])

en = hl.group_words([("Hello", 0, .4), ("there", .45, .8), ("friend", .85, 1.2)], "Hello there, friend!")
rec("Q2", "Tiếng có dấu cách (Anh/Việt) ghép đúng khoảng trắng", [s[2] for s in en] == ["Hello there, friend!"],
    [s[2] for s in en])

long_items = [(c, i * 0.25, i * 0.25 + 0.2) for i, c in enumerate("一二三四五六七八九十" * 4)]
ls = hl.group_words(long_items, "一二三四五六七八九十" * 4)
rec("Q3", "Câu không dấu quá dài bị chia ≤ 6.5 s", all(e - s <= 6.8 for s, e, *_ in ls) and len(ls) >= 2,
    [(round(s, 1), round(e, 1)) for s, e, *_ in ls])


# ---------------------------------------------------------------- Q4 chạy qua Transcriber với model giả lập
class _TS:
    def __init__(self, text, s, e):
        self.text, self.start_time, self.end_time = text, s, e


class FakeQwen:
    calls: list = []

    @classmethod
    def from_pretrained(cls, model_id, **kw):
        cls.kw = kw
        return cls()

    def transcribe(self, audio, language, return_time_stamps):
        arr, sr = audio[0]
        dur = len(arr) / sr
        FakeQwen.calls.append((round(dur, 1), language[0], return_time_stamps))
        txt = "你好。再见。"
        return [types.SimpleNamespace(language=language[0], text=txt,
                                      time_stamps=[_TS("你", 1.0, 1.2), _TS("好", 1.2, 1.4),
                                                   _TS("再", dur - 2, dur - 1.8), _TS("见", dur - 1.8, dur - 1.6)])]


sys.modules["qwen_asr"] = types.SimpleNamespace(Qwen3ASRModel=FakeQwen)
fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False),
                                   bfloat16="bf16", float32="fp32")
hl._torch = lambda: fake_torch
hl.device_name = lambda: "CPU (giả lập)"
vid = QA / "asr_500s.mp4"
if not vid.exists():
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=500", "-c:a", "aac", str(vid)], check=True)
tc = Transcriber([], "Tiếng Trung", engine="Qwen/Qwen3-ASR-1.7B", log=lambda *_: None)
cues = tc.run(str(vid))
wins = FakeQwen.calls
ok = (len(cues) >= 4 and all(d <= 240.05 for d, *_ in wins) and all(l == "Chinese" and r for _, l, r in wins)
      and FakeQwen.kw.get("forced_aligner") == hl.ALIGNER)
rec("Q4", "Transcriber dùng Qwen3-ASR: cửa sổ ≤ 240 s, tiếng Trung, có căn giờ, ghép đủ câu", ok,
    f"{len(cues)} câu, cửa sổ đã gửi (giây, ngôn ngữ, căn giờ)={wins}, câu đầu={cues[0].start}→{cues[0].end} "
    f"'{cues[0].text}', câu cuối={cues[-1].start}")
starts = [c.start for c in cues]
rec("Q5", "Thời gian câu tăng dần, không chồng nhau qua các cửa sổ", starts == sorted(starts)
    and all(a.end <= b.start for a, b in zip(cues, cues[1:])), starts)

# ---------------------------------------------------------------- R1/R2 BS-RoFormer thật
from app import roformer  # noqa: E402
why = roformer.available()
if why:
    rec("R1", "BS-RoFormer tách giọng thật", None, f"bỏ qua: {why}")
else:
    from app.vocal import load_audio, models_dir
    src = QA / "rof_mix.wav"
    if not src.exists():   # “giọng” (sin điều biên) + “nhạc” (hợp âm) 4 s (CPU rất chậm)
        sr = 44100; tt = np.arange(sr * 4) / sr
        voice = 0.3 * np.sin(2 * np.pi * 220 * tt) * (np.sin(2 * np.pi * 3 * tt) > 0)
        music = 0.15 * (np.sin(2 * np.pi * 110 * tt) + np.sin(2 * np.pi * 165 * tt))
        from app.vocal import save_wav
        save_wav(str(src), np.stack([voice + music] * 2).astype(np.float32), sr)
    inst, voc = str(QA / "rof_inst.wav"), str(QA / "rof_voc.wav")
    prog = []
    t0 = time.monotonic()
    try:
        dev = roformer.separate_file(str(src), inst, voc, str(models_dir()), log=print,
                                     progress=lambda d, t: prog.append((d, t)))
        a, b = load_audio(inst), load_audio(voc)
        ok = a.shape[0] == 2 and abs(a.shape[1] - 44100 * 4) < 4410 and abs(b.shape[1] - a.shape[1]) < 10
        rec("R1", "BS-RoFormer tách giọng thật -> 2 file đúng định dạng", ok,
            f"{dev}, {time.monotonic() - t0:.0f}s cho 4s âm thanh, tiến độ nhận {len(prog)} lần, "
            f"nhạc nền {a.shape}, giọng {b.shape}")
    except Exception as e:  # noqa: BLE001
        rec("R1", "BS-RoFormer tách giọng thật", False, f"{type(e).__name__}: {str(e)[-400:]}")
    stop = threading.Event()
    threading.Timer(4.0, stop.set).start()
    t0 = time.monotonic()
    try:
        roformer.separate_file(str(QA / "long.mp4") if (QA / "long.mp4").exists() else str(src), inst, voc,
                               str(models_dir()), log=lambda *_: None, stop=stop)
        rec("R2", "Dừng BS-RoFormer giữa chừng", False, "chạy hết, không dừng")
    except roformer.Stopped:
        lag = time.monotonic() - t0 - 4.0
        left = subprocess.run(["pgrep", "-f", "app.roformer import _child"], capture_output=True, text=True).stdout.split()
        rec("R2", "Dừng BS-RoFormer giữa chừng (kill tiến trình con)", lag < 2 and not left,
            f"dừng sau {lag:.1f}s kể từ lúc bấm, tiến trình con còn lại={left or 'không'}")

print(f"\n{sum(RES)}/{len(RES)} PASS (SKIP tính là PASS)")
sys.exit(0 if all(RES) else 1)
