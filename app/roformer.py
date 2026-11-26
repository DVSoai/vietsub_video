"""Tách giọng chất lượng cao bằng BS-RoFormer (qua thư viện audio-separator, MIT).

Chạy trong một tiến trình con để nút Dừng có tác dụng ngay (kill được) và để model ~640 MB
không nằm lại trong RAM của app sau khi tách xong.

Cần: pip install "audio-separator[gpu]" (máy có card NVIDIA) hoặc audio-separator (CPU) + torch.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading

MODEL_FILE = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"     # BS-Roformer-Viperx-1297: vocals 12.9, instrumental 17.0
MODEL_LABEL = "BS-RoFormer (Viperx 1297)"


from .vocal import Stopped  # noqa: E402  (dùng chung để DubWorker nhận ra “Đã dừng”)


def available() -> str:
    """'' nếu dùng được, ngược lại là lý do."""
    try:
        import audio_separator  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return ("Chưa cài BS-RoFormer. Chạy cai_long_tieng_gpu.bat (đã gồm audio-separator) rồi mở lại app.")
    return ""


def _child(wav: str, out_dir: str, model_dir: str):
    """Chạy trong tiến trình con: tách và in JSON kết quả ở dòng cuối."""
    import logging
    from audio_separator.separator import Separator
    sep = Separator(log_level=logging.WARNING, model_file_dir=model_dir, output_dir=out_dir, output_format="WAV")
    sep.load_model(model_filename=MODEL_FILE)
    files = sep.separate(wav, {"Vocals": "vocals", "Instrumental": "instrumental"})
    dev = "GPU (CUDA)"
    try:
        import torch
        if not torch.cuda.is_available():
            dev = "CPU"
    except Exception:  # noqa: BLE001
        dev = "?"
    print("@@RESULT@@" + json.dumps({"files": [os.path.join(out_dir, os.path.basename(f)) for f in files],
                                    "device": dev}), flush=True)


def separate_file(media: str, out_inst: str, out_voc: str, model_dir: str, log=print,
                  progress=lambda d, t: None, stop: threading.Event | None = None) -> str:
    """Tách media -> 2 file WAV (nhạc nền, giọng). Trả về thiết bị đã dùng."""
    why = available()
    if why:
        raise RuntimeError(why)
    from .vocal import load_audio, save_wav
    os.makedirs(model_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="st_roformer_")
    try:
        wav = os.path.join(tmp, "mix.wav")
        save_wav(wav, load_audio(media))             # 44.1 kHz stereo như model cần
        if not os.path.exists(os.path.join(model_dir, MODEL_FILE)):
            log(f"  Lần đầu: tải model {MODEL_LABEL} (~640 MB)…")
        log(f"  Tách giọng bằng {MODEL_LABEL}…")
        if getattr(sys, "frozen", False):            # bản .exe: chạy trong tiến trình (không dừng giữa chừng được)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                _child(wav, tmp, model_dir)
            out = buf.getvalue()
        else:
            out = _run_child(wav, tmp, model_dir, progress, stop)
        m = re.search(r"@@RESULT@@(.*)", out)
        if not m:
            raise RuntimeError("BS-RoFormer không trả kết quả:\n" + out[-800:])
        res = json.loads(m.group(1))
        voc = next((f for f in res["files"] if "vocals" in os.path.basename(f).lower()), None)
        inst = next((f for f in res["files"] if "instrumental" in os.path.basename(f).lower()), None)
        if not (voc and inst and os.path.exists(voc) and os.path.exists(inst)):
            raise RuntimeError(f"Thiếu file sau khi tách: {res['files']}")
        # đổi về đúng định dạng app dùng (float32 44.1 kHz stereo)
        save_wav(out_inst, load_audio(inst)); save_wav(out_voc, load_audio(voc))
        progress(1, 1)
        return res["device"]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _run_child(wav, out_dir, model_dir, progress, stop) -> str:
    code = ("import sys; sys.path.insert(0, sys.argv[4]); from app.roformer import _child; "
            "_child(sys.argv[1], sys.argv[2], sys.argv[3])")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flags = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.Popen([sys.executable, "-c", code, wav, out_dir, model_dir, root],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=flags, bufsize=1)
    out_lines: list[str] = []
    err_tail: list[str] = []

    def read_out():
        for line in proc.stdout:
            out_lines.append(line)

    def read_err():                                   # tqdm in tiến độ ra stderr: "  37%|███ …"
        buf = ""
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            if ch in "\r\n":
                m = re.search(r"(\d{1,3})%\|", buf)
                if m:
                    progress(int(m.group(1)), 100)
                elif buf.strip():
                    err_tail.append(buf.strip()); del err_tail[:-30]
                buf = ""
            else:
                buf += ch
    ts = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in ts:
        t.start()
    while proc.poll() is None:
        if stop is not None and stop.wait(0.2):
            proc.kill(); proc.wait()
            raise Stopped()
        elif stop is None:
            proc.wait()
    for t in ts:
        t.join(2)
    out = "".join(out_lines)
    if proc.returncode != 0:
        raise RuntimeError("BS-RoFormer lỗi:\n" + "\n".join(err_tail[-8:]))
    return out
