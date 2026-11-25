"""Tách giọng nói khỏi nhạc nền bằng model MDX-Net (bộ UVR), chạy bằng ONNX Runtime.

Ưu tiên card NVIDIA (CUDA), không được thì tự chạy CPU. Kết quả:
  - instrumental: nhạc nền + tiếng động (đã bỏ giọng)  -> dùng để trộn lồng tiếng
  - vocals:       chỉ giọng nói                         -> nghe giọng tạo phụ đề sạch hơn
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

MODEL_NAME = "UVR-MDX-NET-Inst_HQ_3.onnx"
MODEL_URL = ("https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/" + MODEL_NAME)
SR = 44100
HOP = 1024


class Stopped(Exception):
    pass


def models_dir() -> Path:
    from .gui import app_dir
    d = app_dir() / "data" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_model(progress=lambda done, total: None) -> str:
    path = models_dir() / MODEL_NAME
    if path.exists() and path.stat().st_size > 10_000_000:
        return str(path)
    import requests
    tmp = str(path) + ".part"
    with requests.get(MODEL_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    os.replace(tmp, path)
    return str(path)


def _session(model_path: str):
    import onnxruntime as ort
    try:
        ort.preload_dlls()  # dùng DLL CUDA/cuDNN đi kèm PyTorch (nếu có)
    except Exception:  # noqa: BLE001
        pass
    avail = ort.get_available_providers()
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 4) - 1)
    if "CUDAExecutionProvider" in avail:
        try:
            s = ort.InferenceSession(model_path, so, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            if "CUDAExecutionProvider" in s.get_providers():
                return s, "GPU (CUDA)"
        except Exception:  # noqa: BLE001
            pass
    return ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"]), "CPU"


def _ffmpeg() -> str:
    from .transcribe import ffmpeg_exe
    return ffmpeg_exe()


def _flags():
    return 0x08000000 if sys.platform == "win32" else 0


def load_audio(path: str, sr: int = SR, channels: int = 2) -> np.ndarray:
    """Đọc âm thanh bất kỳ (kể cả video) -> float32 [channels, n]."""
    r = subprocess.run([_ffmpeg(), "-v", "error", "-i", path, "-vn", "-ac", str(channels), "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, creationflags=_flags())
    if r.returncode != 0:
        raise RuntimeError("ffmpeg không đọc được âm thanh: " + r.stderr.decode("utf-8", "replace")[-300:])
    a = np.frombuffer(r.stdout, dtype=np.float32)
    return a.reshape(-1, channels).T.copy()


def save_wav(path: str, audio: np.ndarray, sr: int = SR):
    """audio: [channels, n] hoặc [n] float32 -> WAV 16-bit."""
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[None]
    pcm = (np.clip(a.T, -1, 1) * 32767).astype("<i2")
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(a.shape[0]); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


class Separator:
    """MDX-Net: STFT -> model đoán phổ nhạc nền -> ISTFT; giọng = gốc - nhạc nền."""

    def __init__(self, model_path: str | None = None, log=print):
        self.model_path = model_path or ensure_model()
        self.sess, self.device = _session(self.model_path)
        inp = self.sess.get_inputs()[0]
        self.in_name = inp.name
        self.dim_f = int(inp.shape[2])
        self.dim_t = int(inp.shape[3])
        self.n_fft = self.dim_f * 2              # Inst_HQ_3: dim_f 3072, n_fft 6144
        self.chunk = HOP * (self.dim_t - 1)
        self.trim = self.n_fft // 2
        self.gen = self.chunk - 2 * self.trim
        self.window = np.hanning(self.n_fft + 1)[:-1].astype(np.float32)  # Hann tuần hoàn
        self.compensate = 1.022
        log(f"  Tách giọng chạy bằng {self.device}")

    # --- STFT/ISTFT (center=True, giống torch.stft) ---
    def _stft(self, x: np.ndarray) -> np.ndarray:          # x [2, chunk] -> [4, dim_f, dim_t]
        pad = self.n_fft // 2
        xp = np.pad(x, ((0, 0), (pad, pad)), mode="reflect")
        idx = np.arange(self.dim_t)[:, None] * HOP + np.arange(self.n_fft)[None, :]
        frames = xp[:, idx] * self.window                   # [2, T, n_fft]
        spec = np.fft.rfft(frames, axis=-1)[:, :, :self.dim_f]   # [2, T, F]
        spec = spec.transpose(0, 2, 1)                       # [2, F, T]
        out = np.empty((4, self.dim_f, self.dim_t), np.float32)
        out[0::2] = spec.real
        out[1::2] = spec.imag
        return out

    def _istft(self, s: np.ndarray) -> np.ndarray:         # [4, F, T] -> [2, chunk]
        nb = self.n_fft // 2 + 1
        c = np.zeros((2, nb, self.dim_t), np.complex64)
        c[:, :self.dim_f] = s[0::2] + 1j * s[1::2]
        frames = np.fft.irfft(c.transpose(0, 2, 1), n=self.n_fft, axis=-1) * self.window   # [2, T, n_fft]
        length = self.n_fft + HOP * (self.dim_t - 1)
        out = np.zeros((2, length), np.float32)
        wsum = np.zeros(length, np.float32)
        w2 = self.window ** 2
        for t in range(self.dim_t):
            out[:, t * HOP:t * HOP + self.n_fft] += frames[:, t]
            wsum[t * HOP:t * HOP + self.n_fft] += w2
        out /= np.maximum(wsum, 1e-8)
        pad = self.n_fft // 2
        return out[:, pad:pad + self.chunk]

    def separate(self, mix: np.ndarray, progress=lambda d, t: None, stop: threading.Event | None = None,
                 batch: int = 4) -> tuple[np.ndarray, np.ndarray]:
        """mix [2, n] 44.1 kHz -> (instrumental, vocals)."""
        n = mix.shape[1]
        pad = self.gen + self.trim - (n % self.gen)
        mixture = np.concatenate([np.zeros((2, self.trim), np.float32), mix, np.zeros((2, pad), np.float32)], axis=1)
        starts = list(range(0, mixture.shape[1] - self.chunk + 1, self.gen))
        inst = np.zeros((2, len(starts) * self.gen), np.float32)
        for b0 in range(0, len(starts), batch):
            if stop is not None and stop.is_set():
                raise Stopped()
            group = starts[b0:b0 + batch]
            specs = np.stack([self._stft(mixture[:, s:s + self.chunk]) for s in group])
            pred = self.sess.run(None, {self.in_name: specs})[0]
            for k, s in enumerate(group):
                wav = self._istft(pred[k])
                i = (b0 + k) * self.gen
                inst[:, i:i + self.gen] = wav[:, self.trim:self.trim + self.gen]
            progress(min(len(starts), b0 + batch), len(starts))
        inst = inst[:, :n] * self.compensate
        vocals = mix - inst
        return inst, vocals


def separate_file(media: str, out_inst: str, out_voc: str, log=print, progress=lambda d, t: None,
                  stop: threading.Event | None = None) -> str:
    """Tách giọng một video/âm thanh -> 2 file WAV. Trả về thiết bị đã dùng."""
    log("  Đang tải model tách giọng (lần đầu ~60 MB)…" if not (models_dir() / MODEL_NAME).exists() else "  Đang nạp model tách giọng…")
    sep = Separator(log=log)
    mix = load_audio(media)
    inst, voc = sep.separate(mix, progress=progress, stop=stop)
    save_wav(out_inst, inst)
    save_wav(out_voc, voc)
    return sep.device
