"""Kiểm thử thực tế chức năng Lồng tiếng (chạy app thật, offscreen).

Giả lập: VieNeu (không tải được model HF trong môi trường test) và Gemini/Vertex (mock server :8765).
Thật: ffmpeg, MDX-Net tách giọng, toàn bộ DubWorker/Exporter/Qt.

Chạy (từ thư mục gốc repo):
    python tests/mock_ai_server.py &          # Gemini/Vertex giả lập ở cổng 8765
    python tests/qa_dubbing.py <nhãn> [T2,T5,...]
Video/phụ đề kiểm thử tự tạo trong tests/qa_data/ lần đầu. Kết quả: tests/qa_data/results_<nhãn>.json
Lần đầu tách giọng sẽ tải model MDX-Net (~65 MB) vào data/models.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

QA = Path(__file__).resolve().parent / "qa_data"
QA.mkdir(exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NO_PROXY"] = "127.0.0.1"

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.gui as g  # noqa: E402
import app.vocal as vocal  # noqa: E402
from app import theme  # noqa: E402
from app.transcribe import ffmpeg_exe  # noqa: E402

FF = ffmpeg_exe()
RESULTS: list[dict] = []


def make_inputs():
    """Video thử: 1280x720 40s có 'chữ cứng' (hộp trắng chữ đen) + video dài 240s + phụ đề Trung + logo."""
    if not (QA / "hard.mp4").exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=40", "-f", "lavfi",
                        "-i", "sine=f=300:d=40", "-vf", "drawbox=x=240:y=580:w=800:h=70:color=white:t=fill,"
                        "drawbox=x=280:y=600:w=720:h=30:color=black:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(QA / "hard.mp4")], check=True)
    if not (QA / "long.mp4").exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=240", "-f", "lavfi",
                        "-i", "sine=f=300:d=240", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                        str(QA / "long.mp4")], check=True)
    if not (QA / "logo.png").exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x80,format=rgba", "-frames:v", "1",
                        str(QA / "logo.png")], check=True)
    lines = [(1, 3), (4, 6), (7.5, 9), (11, 13.5), (15, 17), (20, 22), (25, 27.5), (31, 34)]
    zh = ["师尊，弟子来迟了。", "嗯。", "林澈，你可知罪？", "弟子知罪。", "今日便罚你面壁三月。", "师尊开恩！", "不必多言。", "是，弟子遵命。"]

    def ts(x):
        ms = int(x * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    (QA / "hard.zh.srt").write_text("\n".join(f"{i + 1}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, ((a, b), t)
                                              in enumerate(zip(lines, zh))), encoding="utf-8")


make_inputs()


def rec(tid, name, ok, detail):
    RESULTS.append({"id": tid, "name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


def probe_dur(path, stream=None):
    args = ["ffprobe", "-v", "error"]
    if stream:
        args += ["-select_streams", stream, "-show_entries", "stream=duration"]
    else:
        args += ["-show_entries", "format=duration"]
    out = subprocess.run(args + ["-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip().splitlines()
    try:
        return float(out[0])
    except (IndexError, ValueError):
        return -1.0


def pcm(path, sr=16000):
    r = subprocess.run([FF, "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, np.float32)


def onsets(x, sr=16000, thr=0.03, min_gap=0.04):
    """Thời điểm bắt đầu các đoạn có tiếng (tách bởi khoảng lặng ≥ min_gap)."""
    win = int(0.01 * sr)
    env = np.array([np.abs(x[i:i + win]).max() for i in range(0, len(x) - win, win)])
    on = env > thr
    res, last_end = [], -1e9
    for i, v in enumerate(on):
        t = i * 0.01
        if v:
            if t - last_end > min_gap and (not res or t - res[-1] > min_gap):
                if i == 0 or not on[i - 1]:
                    res.append(t)
            last_end = t
    return res


def ffmpeg_children():
    r = subprocess.run(["pgrep", "-f", "ffmpeg"], capture_output=True, text=True)
    me = str(os.getpid())
    return [p for p in r.stdout.split() if p != me]


# ---------------------------------------------------------------- giả lập VieNeu (đếm số lần đọc)
class FakeVieneu:
    sample_rate = 48000
    V = [("⭐ Hải Đăng (Nam miền Bắc)", "hai_dang"), ("⭐ Ngọc Huyền (Nữ miền Bắc)", "ngoc_huyen"),
         ("Minh Quân (Nam miền Nam)", "minh_quan"), ("Thảo Vy (Nữ miền Nam)", "thao_vy")]
    FREQ = {"hai_dang": 180, "ngoc_huyen": 320, "minh_quan": 140, "thao_vy": 280}
    calls: list = []

    def list_preset_voices(self):
        return self.V

    def infer(self, text, voice=None):
        FakeVieneu.calls.append((text, voice))
        n = int(48000 * (0.25 + 0.06 * len(text)))
        t = np.arange(n) / 48000
        env = np.minimum(1, np.minimum(t, t[::-1]) * 40)
        return (0.4 * np.sin(2 * np.pi * self.FREQ.get(voice, 220) * t) * env).astype(np.float32)


# ---------------------------------------------------------------- dựng app
DATA = QA / f"data_{LABEL}"
shutil.rmtree(DATA, ignore_errors=True)
g.DATA = DATA; g.PROMPTS = DATA / "prompts"; g.CONFIG = DATA / "settings.json"
g.PROVIDERS["Vertex AI (API key)"]["base_url"] = "http://127.0.0.1:8765/vxa"

app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())
w = g.MainWindow(); w.show()
w.cb_provider.setCurrentText("Vertex AI (API key)"); w.ed_keys.setPlainText("k"); w.cb_model.setCurrentText("gemini-x")
w.chk_use.setChecked(True)
d = w.dub_page
d.engine.tts = FakeVieneu(); d.engine.sr = 48000; d.engine.device = "CPU (giả lập)"
d.voice_list = FakeVieneu.V
MAIN = threading.main_thread()

# --- theo dõi hàm đọc widget bị gọi từ luồng nền
OFF_THREAD: dict[str, int] = {}


def watch(obj, name):
    orig = getattr(obj, name)

    def wrapper(*a, **k):
        if threading.current_thread() is not MAIN:
            OFF_THREAD[name] = OFF_THREAD.get(name, 0) + 1
        return orig(*a, **k)
    setattr(obj, name, wrapper)


for nm in ("find_srt", "dub_audio_path", "dub_video_path", "providers", "hints", "visual_settings",
           "mix_options", "out_dir_getter"):
    if hasattr(d, nm):
        watch(d, nm)
for nm in ("_look", "_style", "_opts"):
    watch(w.render_page, nm)


def mock_calls():
    import urllib.request
    return json.loads(urllib.request.urlopen("http://127.0.0.1:8765/stats", timeout=3).read())["calls"]


def run(mode, only=None, stop_after=None, timeout=900):
    loop = QEventLoop()
    res = {}
    d._run(mode, only)
    if d.worker is None or not d.worker.isRunning():
        return {"out": "", "err": "không chạy được", "secs": 0}
    t0 = time.monotonic()

    def done(o, e):
        res.update(out=o, err=e, secs=time.monotonic() - t0)
        QTimer.singleShot(30, loop.quit)
    d.worker.done.connect(done)
    if stop_after is not None:
        QTimer.singleShot(int(stop_after * 1000), lambda: (res.__setitem__("stop_at", time.monotonic() - t0),
                                                           d.btn_stop.click()))
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()
    d.worker.wait(10000)
    app.processEvents()
    return res


def fresh(video):
    p = Path(video)
    for f in (p.with_name(p.stem + "_long_tieng"),):
        shutil.rmtree(f, ignore_errors=True)
    for f in p.parent.glob(p.stem + ".vi.dub*.m4a"):
        f.unlink()
    for f in p.parent.glob(p.stem + ".long_tieng*"):
        f.unlink()


def want(tid):
    return ONLY is None or tid in ONLY


HARD = str(QA / "hard.mp4")
LONG = str(QA / "long.mp4")
w._go_page(2)
d.sl_bg.setValue(0)              # chỉ còn giọng -> đo được vị trí từng câu
d.chk_skip_review.setChecked(False)
d.chk_export.setChecked(True)

# ================================================================ T1 chạy tự động (tách giọng thật)
fresh(HARD)
d.set_video(HARD)
d.rb_remove.setChecked(True)
c0 = mock_calls()
r = run("auto")
ok = r.get("out") == "review" and len(d.project.lines) == 8 and len(d.project.characters) >= 2
rec("T1", "Làm tự động tới bước kiểm tra giọng", ok,
    f"kết quả={r.get('out') or r.get('err')}, {len(d.project.lines)} câu, nhân vật="
    f"{[(c.name, c.voice) for c in d.project.characters]}, {r.get('secs', 0):.0f}s, gọi AI={mock_calls() - c0}")

# ================================================================ T2 giữ giọng đã chọn khi chạy tự động lại
if want("T2"):
    ch = d.project.char("Sư tôn")
    ch.voice, ch.speed, ch.pitch, ch.volume = "thao_vy", 1.2, 2.0, 1.3
    d.project.save()
    r = run("auto")
    ch2 = d.project.char("Sư tôn")
    got = (ch2.voice, ch2.speed, ch2.pitch, ch2.volume) if ch2 else None
    rec("T2", "Giữ giọng/tốc độ/cao độ/âm lượng nhân vật khi chạy tự động lại", got == ("thao_vy", 1.2, 2.0, 1.3),
        f"trước=('thao_vy',1.2,2.0,1.3) sau={got}")
    # sửa câu trong bảng rồi chạy lại tự động (không bật dịch lại) -> câu sửa phải còn
    d.project.lines[0].text = "Sư tôn, đệ tử đến muộn."
    d.project.save()
    r = run("auto")
    rec("T2b", "Giữ câu đã sửa tay khi chạy tự động lại (không bật dịch lại)",
        d.project.lines[0].text == "Sư tôn, đệ tử đến muộn.", f"câu 1 sau khi chạy lại='{d.project.lines[0].text}'")

# ================================================================ T3 cache TTS
if want("T3"):
    d.sp_vspeed.setValue(1.0)
    FakeVieneu.calls.clear(); r = run("voice"); n_first = len(FakeVieneu.calls)
    FakeVieneu.calls.clear(); r = run("voice"); n_again = len(FakeVieneu.calls)
    d.project.lines[2].text = d.project.lines[2].text + " Thật sao?"; d.project.save()
    FakeVieneu.calls.clear(); r = run("voice"); n_edit = len(FakeVieneu.calls)
    lam = d.project.char("Lâm Triệt"); n_lam = sum(1 for l in d.project.lines if l.speaker == "Lâm Triệt")
    lam.voice = "minh_quan"; d.project.save()
    FakeVieneu.calls.clear(); r = run("voice"); n_voice = len(FakeVieneu.calls)
    lam.speed, lam.pitch = 1.15, -2.0; d.project.save()
    FakeVieneu.calls.clear(); r = run("voice"); n_fx = len(FakeVieneu.calls)
    # chọn 1 câu để tạo lại
    FakeVieneu.calls.clear(); r = run("voice", only={d.project.lines[1].index}); n_only = len(FakeVieneu.calls)
    ok = (n_first == 8 and n_again == 0 and n_edit == 1 and n_voice == n_lam and n_fx == 0 and n_only == 1)
    rec("T3", "Cache TTS: chạy lại / sửa câu / đổi giọng / đổi tốc độ-cao độ / tạo lại 1 câu", ok,
        f"đọc lần đầu={n_first}, chạy lại={n_again}, sửa 1 câu={n_edit}, đổi giọng Lâm Triệt={n_voice} "
        f"(Lâm Triệt có {n_lam} câu), đổi tốc độ/cao độ={n_fx}, tạo lại 1 câu={n_only}")

# ================================================================ T4 đồng bộ tốc độ video
if want("T4"):
    for vs in (0.5, 1.0, 1.25, 2.0):
        d.sp_vspeed.setValue(vs)
        r = run("voice")
        out = d.dub_video_path()
        vd, ad = probe_dur(out, "v:0"), probe_dur(out, "a:0")
        exp = 40 / vs
        x = pcm(out)
        got = onsets(x)
        want_t = sorted(l.start / vs for l in d.project.lines)
        # mỗi câu mong đợi phải có một điểm bắt đầu tiếng gần nó (≤ 80 ms, hoặc lùi ≤ 0.7s khi câu trước dài)
        errs = []
        for t in want_t:
            near = [o - t for o in got if -0.08 <= o - t <= 0.7]
            errs.append(min(near, key=abs) if near else None)
        miss = sum(1 for e in errs if e is None)
        maxerr = max((abs(e) for e in errs if e is not None), default=0)
        ok = abs(vd - exp) < 0.25 and abs(ad - exp) < 0.35 and miss == 0
        rec(f"T4@{vs}", f"Đồng bộ hình/tiếng/câu thoại ở tốc độ {vs}x", ok,
            f"video={vd:.2f}s audio={ad:.2f}s mong đợi={exp:.2f}s, câu lệch tối đa={maxerr * 1000:.0f}ms, "
            f"câu không thấy={miss}/{len(want_t)}, lỗi={r.get('err')}"
            + (f" | mong đợi={[round(t, 2) for t in want_t]} thấy={[round(o, 2) for o in got]}" if miss else ""))
        srt_out = Path(out).with_suffix("").as_posix() + ".vi.srt"
        if vs != 1.0 and os.path.exists(srt_out):
            from app import srt as S
            from app.dub import ts_seconds
            cs = S.parse(S.read_text(srt_out))
            dev = max(abs(ts_seconds(c.start) - t) for c, t in zip(cs, want_t))
            rec(f"T4s@{vs}", f"Phụ đề Việt khớp tốc độ {vs}x", dev < 0.01, f"lệch tối đa={dev * 1000:.1f}ms")

# ================================================================ T12 câu dài không chồng lên nhau
def tone_energy(x, f, sr=16000, win=0.05):
    n = int(win * sr); t = np.arange(n) / sr
    c, s_ = np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)
    return np.array([np.hypot((x[i:i + n] * c).sum(), (x[i:i + n] * s_).sum()) / n for i in range(0, len(x) - n, n)])


if want("T12"):
    d.sp_vspeed.setValue(1.0)
    saved = [(l.text, l.speaker) for l in d.project.lines]
    for c in d.project.characters:
        c.speed, c.pitch, c.volume = 1.0, 0.0, 1.0
    d.project.char("Lâm Triệt").voice = "hai_dang"; d.project.char("Sư tôn").voice = "ngoc_huyen"
    l1, l2 = d.project.lines[0], d.project.lines[1]
    l1.speaker, l2.speaker = "Lâm Triệt", "Sư tôn"
    l1.text = "Sư tôn, đệ tử đến muộn vì trên đường gặp yêu thú chặn lối, đánh mãi mới thoát được."  # rất dài cho 3s
    d.project.save()
    r = run("voice")
    x = pcm(d.dub_video_path())
    e1, e2 = tone_energy(x, 180), tone_energy(x, 320)
    both = int(((e1 > 0.02) & (e2 > 0.02)).sum())
    rec("T12", "Câu quá dài không chồng lên câu sau", both == 0,
        f"số khung 50ms có 2 giọng cùng lúc={both} ({both * 50} ms)")
    for l, (t_, s_) in zip(d.project.lines, saved):
        l.text, l.speaker = t_, s_
    d.project.save()

# ================================================================ T13 đã có .zh.srt + .vi.srt, không có nhà cung cấp AI
if want("T13"):
    w.chk_use.setChecked(False); w.ed_keys.setPlainText("")
    cfg_pp = w.cfg.get("per_provider", {})
    for k in cfg_pp.values():
        k["enabled"] = False
    w._save_provider_state(w.cb_provider.currentText())
    try:
        w._make_translate_providers(); has = True
    except Exception:  # noqa: BLE001
        has = False
    pj = Path(d.workdir) / "project.json"
    if pj.exists():
        pj.unlink()
    d.project = type(d.project).load(d.video, d.workdir)
    r = run("auto")
    rec("T13", "Chạy tự động khi đã có phụ đề, không có nhà cung cấp AI", r.get("out") == "review",
        f"có provider={has}, kết quả={r.get('out') or r.get('err')}, nhân vật={[c.name for c in d.project.characters]}")
    w.ed_keys.setPlainText("k"); w.chk_use.setChecked(True)

# ================================================================ T5 cache âm thanh nhiều tốc độ + T6 export tái sử dụng
if want("T5"):
    d.sp_vspeed.setValue(1.25); run("voice")
    a125 = d.dub_audio_path(); dur125 = probe_dur(a125)
    d.sp_vspeed.setValue(1.5); run("voice")
    a15 = d.dub_audio_path()
    d.sp_vspeed.setValue(1.25)
    FakeVieneu.calls.clear(); c0 = mock_calls()
    r = run("export")
    out = d.dub_video_path()
    vd, ad = probe_dur(out, "v:0"), probe_dur(out, "a:0")
    rec("T5", "Xuất lại ở 1.25x sau khi đã trộn 1.5x dùng đúng tiếng 1.25x", abs(ad - 32) < 0.4 and abs(vd - 32) < 0.3,
        f"file tiếng 1.25x={Path(a125).name}, 1.5x={Path(a15).name} (trùng tên={a125 == a15}); "
        f"video={vd:.2f}s audio={ad:.2f}s mong đợi 32.00s")
    rec("T6", "Chế độ xuất không gọi lại AI/TTS", len(FakeVieneu.calls) == 0 and mock_calls() == c0,
        f"TTS={len(FakeVieneu.calls)} lần, AI={mock_calls() - c0} lần, {r.get('secs', 0):.1f}s")

# ================================================================ T7 dừng tác vụ
if want("T7"):
    from app.dub import Line, Project, Character
    fresh(LONG)
    d.set_video(LONG)
    d.project.characters = [Character("A", "nam", voice="hai_dang")]
    d.project.lines = [Line(1, 5.0, 7.0, "Thử dừng.", "试", "A")]
    d.project.save()
    # 7a: ghép video có đổi tốc độ (nén lại hình) -> dừng sau 3s
    d.sp_vspeed.setValue(1.3)
    os.makedirs(d.workdir, exist_ok=True)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=200:d={240 / 1.3:.2f}", "-c:a", "aac",
                    d.dub_audio_path()], check=True)
    d.cb_cover.setCurrentIndex(0); d.chk_subs.setChecked(False); d.cb_aspect.setCurrentIndex(0); d.ed_logo.setText("")
    r = run("export", stop_after=3, timeout=600)
    lag = r.get("secs", 0) - r.get("stop_at", 0)
    time.sleep(0.5)
    left = ffmpeg_children()
    rec("T7a", "Dừng khi đang ghép video (đổi tốc độ, không hiệu ứng)", r.get("err") == "Đã dừng" and lag < 4 and not left,
        f"kết quả={r.get('out') or r.get('err')}, dừng sau {lag:.1f}s kể từ lúc bấm, tổng {r.get('secs', 0):.1f}s, "
        f"ffmpeg còn chạy={left}")
    for pid in left:
        subprocess.run(["kill", "-9", pid])
    # 7b: xuất có xoá chữ (Exporter) -> dừng sau 3s
    d.cb_cover.setCurrentIndex(1)
    r = run("export", stop_after=3, timeout=600)
    lag = r.get("secs", 0) - r.get("stop_at", 0)
    time.sleep(0.5)
    left = ffmpeg_children()
    rec("T7b", "Dừng khi đang xuất có xoá chữ (Exporter)", r.get("err") == "Đã dừng" and lag < 4 and not left,
        f"kết quả={r.get('out') or r.get('err')}, dừng sau {lag:.1f}s kể từ lúc bấm, ffmpeg còn chạy={left}")
    for pid in left:
        subprocess.run(["kill", "-9", pid])
    d.cb_cover.setCurrentIndex(0)
    # 7c: đang tách giọng -> dừng
    d.rb_remove.setChecked(True)
    for f in ("instrumental.wav", "vocals.wav"):
        p = Path(d.workdir) / f
        if p.exists():
            p.unlink()
    r = run("voice", stop_after=8, timeout=600)
    lag = r.get("secs", 0) - r.get("stop_at", 0)
    rec("T7c", "Dừng khi đang tách giọng", r.get("err") == "Đã dừng" and lag < 15,
        f"kết quả={r.get('out') or r.get('err')}, dừng sau {lag:.1f}s kể từ lúc bấm")
    d.sp_vspeed.setValue(1.0)
    d.set_video(HARD)

# ================================================================ T8 xem trước = video xuất  /  T9 thứ tự filter
if want("T8"):
    from app.render import Cover, Exporter, ExportOpts, Look, SubStyle, preview_frame
    from app import srt as S
    cues = S.parse(S.read_text(str(QA / "hard.zh.srt")))

    def frame_at(video, t, out):
        subprocess.run([FF, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", out], check=True)

    def psnr(a, b):
        r = subprocess.run([FF, "-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"], capture_output=True, text=True)
        import re
        m = re.search(r"average:([\d.inf]+)", r.stderr)
        return float(m.group(1)) if m and m.group(1) != "inf" else 99.0

    cov = Cover(mode="erase", y_pct=79, h_pct=12, x_pct=17)
    st = SubStyle(font_family="DejaVu Sans")
    for name, lk in (("thường", Look()),
                     ("phóng to+màu+lật", Look(zoom=115, bright=6, sat=130, flip=True, sharpen=20)),
                     ("9:16+logo", Look(aspect="9:16", logo=str(QA / "logo.png")))):
        prev = str(QA / f"prev_{LABEL}.png"); expf = str(QA / f"exp_{LABEL}.png"); vid = str(QA / f"exp_{LABEL}.mp4")
        preview_frame(HARD, cues, st, cov, 5.0, prev, max_w=4000, look=lk)
        Exporter(HARD, cues, st, cov, ExportOpts(crf=14), vid, look=lk, log=lambda *_: None).run()
        frame_at(vid, 5.0, expf)
        p = psnr(prev, expf)
        rec(f"T8 {name}", f"Xem trước khớp video xuất ({name})", p > 30, f"PSNR={p:.1f} dB (≥30 là giống)")

if want("T9"):
    from app.render import Cover, Exporter, ExportOpts, Look, SubStyle, filter_graph
    import re
    st = SubStyle(font_family="DejaVu Sans")
    cov = Cover(mode="box", y_pct=79, h_pct=12, x_pct=17, box_color="#00ff00", box_opacity=100)
    g_ = filter_graph({"w": 1280, "h": 720}, cov, True, Look(zoom=120, flip=True, bright=5, aspect="9:16"))
    order = [k for k in ("drawbox", "hflip", "crop=trunc", "eq=", "ass=", "force_original_aspect_ratio") if k in g_]
    order.sort(key=g_.index)
    rec("T9a", "Thứ tự filter", True, " → ".join(order))
    # vùng che căn ở 100% rồi đổi phóng to 120%: chữ cứng (hộp trắng) có lọt ra ngoài vùng che không?
    vid = str(QA / f"zoom_{LABEL}.mp4"); fr = str(QA / f"zoom_{LABEL}.png")
    Exporter(HARD, [], st, Cover(mode="box", y_pct=79, h_pct=12, x_pct=17, box_color="#000000", box_opacity=100),
             ExportOpts(), vid, look=Look(zoom=120), log=lambda *_: None).run()
    subprocess.run([FF, "-v", "error", "-y", "-ss", "5", "-i", vid, "-frames:v", "1", "-vf",
                    "crop=iw*0.5:ih*0.12:iw*0.25:ih*0.875,format=gray", "-f", "rawvideo", fr], check=True)
    px = np.frombuffer(open(fr, "rb").read(), np.uint8)
    white = float((px > 235).mean())
    rec("T9b", "Đổi phóng to sau khi căn vùng che: chữ cứng không lọt ra", white < 0.02,
        f"tỉ lệ điểm trắng (chữ cứng) ngay dưới vùng che={white * 100:.1f}%")

# ================================================================ T10 đồng bộ cấu hình 2 tab
if want("T10"):
    rp = w.render_page
    rp.set_video(HARD)
    rp.lk_sat.setValue(140); rp.lk_zoom.setValue(110); rp.lk_speed.setValue(1.7); rp.lk_logo.setText("/tmp/x.png")
    d.sp_vspeed.setValue(1.25); d.cb_cover.setCurrentIndex(1); d.ed_logo.setText("")
    vis = d.visual_settings()
    lk = vis["look"]
    ok1 = lk.sat == 140 and lk.zoom == 110 and lk.speed == 1.25 and lk.audio_sped and lk.logo == ""
    rec("T10a", "Lồng tiếng lấy màu/phóng to từ tab Xuất video, tốc độ & logo từ tab Lồng tiếng", ok1,
        f"sat={lk.sat} zoom={lk.zoom} speed={lk.speed} audio_sped={lk.audio_sped} logo='{lk.logo}' "
        f"audio_file={Path(vis['opts'].audio_file).name}")
    w._go_page(2); d._align_in_render(); app.processEvents()
    rp.sp_cy.setValue(70.0); rp.sp_ch.setValue(9.0); rp.cb_cover.setCurrentIndex(1)
    w._go_page(2); app.processEvents()
    got = (d.sp_cy.value(), d.sp_ch.value(), d.cb_cover.currentText())
    rec("T10b", "Căn vùng ở tab Xuất video rồi quay lại: vị trí về tab Lồng tiếng", got[:2] == (70.0, 9.0) and "mờ" in got[2],
        f"sau khi quay lại: y={got[0]} cao={got[1]} chế độ='{got[2]}'")
    rp.lk_sat.setValue(100); rp.lk_zoom.setValue(100); rp.lk_speed.setValue(1.0); rp.lk_logo.setText("")

# ================================================================ T14 vùng chỉnh sửa hình trong xuất lồng tiếng
if want("T14"):
    from app.render import EditRegion
    from dataclasses import asdict
    d.set_video(HARD)
    d.sp_vspeed.setValue(1.25)
    d.cb_cover.setCurrentIndex(0); d.chk_subs.setChecked(False); d.cb_aspect.setCurrentIndex(0); d.ed_logo.setText("")
    d.edit_regions = [EditRegion(30, 20, 10, 10, "box", box_color="#ff0000", start=10, end=20),
                      EditRegion(17, 79, 66, 12, "erase"), EditRegion(60, 20, 10, 10, "blur", enabled=False)]
    d.project.regions = [asdict(r) for r in d.edit_regions]; d.project.save()
    vis = d.visual_settings()
    n_vis = len(vis["look"].regions) if vis else 0
    if not os.path.exists(d.dub_audio_path()):
        run("voice")
    FakeVieneu.calls.clear()
    r = run("export")
    out = d.dub_video_path()

    def px_at(t_orig):
        rr = subprocess.run([FF, "-v", "error", "-ss", f"{t_orig / 1.25:.3f}", "-i", out, "-frames:v", "1", "-vf",
                             "crop=20:20:420:170,format=rgb24", "-f", "rawvideo", "-"], capture_output=True)
        return [int(v) for v in np.frombuffer(rr.stdout, np.uint8).reshape(-1, 3).mean(0)]
    p5, p15, p25 = px_at(5), px_at(15), px_at(25)
    red = lambda p_: p_[0] > 200 and p_[1] < 90 and p_[2] < 90  # noqa: E731
    ok = (n_vis == 2 and red(p15) and not red(p5) and not red(p25) and len(FakeVieneu.calls) == 0
          and abs(probe_dur(out, "v:0") - 32) < 0.3)
    rec("T14", "Vùng chỉnh sửa (có khoảng thời gian) áp đúng khi xuất lồng tiếng 1.25x", ok,
        f"vùng gửi sang xuất={n_vis} (1 vùng tắt bị bỏ), RGB lúc 5s={p5} 15s={p15} 25s={p25}, "
        f"TTS gọi lại={len(FakeVieneu.calls)}, video={probe_dur(out, 'v:0'):.2f}s")
    d.set_video(str(QA / "long.mp4")); n_other = len(d.edit_regions)
    d.set_video(HARD); n_back = len(d.edit_regions)
    rec("T14b", "Vùng lưu theo từng video", n_other == 0 and n_back == 3, f"video khác={n_other} vùng, mở lại={n_back} vùng")
    d.edit_regions = []; d.project.regions = []; d.project.save()

rec("T11", "DubWorker không gọi hàm đọc widget từ luồng nền", not OFF_THREAD,
    f"gọi từ luồng nền: {OFF_THREAD or 'không có'}")

json.dump(RESULTS, open(QA / f"results_{LABEL}.json", "w"), ensure_ascii=False, indent=1)
print(f"\n{sum(r['ok'] for r in RESULTS)}/{len(RESULTS)} PASS")
w.close()
