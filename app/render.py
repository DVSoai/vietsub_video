"""Xuất video Vietsub: in phụ đề (kiểu chữ như CapCut) vào video + che chữ cứng có sẵn.

SRT -> ASS (font, cỡ, màu, viền, bóng, nền, vị trí) -> ffmpeg:
    [che chữ cũ: làm mờ / hộp màu] -> ass (libass) -> x264 / NVENC.
Font được chép kèm vào thư mục tạm (fontsdir) nên không phụ thuộc fontconfig của máy.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import srt
from .transcribe import ffmpeg_exe

FONT_EXT = {".ttf", ".otf", ".ttc"}


# ------------------------------------------------------------------ kiểu chữ
@dataclass
class SubStyle:
    font_family: str = "Arial"
    font_files: list = field(default_factory=list)  # các file của họ font (chép kèm khi xuất)
    size_pct: float = 5.2        # cỡ chữ = % chiều cao video
    bold: bool = True
    italic: bool = False
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_pct: float = 0.28    # độ dày viền = % chiều cao video
    shadow_pct: float = 0.0
    box: bool = False            # nền hộp sau chữ
    box_color: str = "#000000"
    box_opacity: int = 60        # %
    margin_pct: float = 7.0      # khoảng cách từ đáy video (%)
    align: str = "bottom"        # bottom | top


PRESETS: dict[str, dict] = {
    "Trắng viền đen (mặc định)": {},
    "Vàng viền đen": {"text_color": "#FFE14D"},
    "Nền đen mờ": {"box": True, "box_opacity": 55, "outline_pct": 0.0, "bold": False},
    "Trắng bóng mềm": {"outline_pct": 0.12, "shadow_pct": 0.35, "bold": False},
    "Cổ trang vàng kim": {"text_color": "#F5D58A", "outline_color": "#3A1E05", "outline_pct": 0.32},
    "Chữ to TikTok": {"size_pct": 6.6, "outline_pct": 0.42, "margin_pct": 14.0},
}


@dataclass
class Cover:
    """Che chữ cứng có sẵn trong video (vd: phụ đề tiếng Trung in sẵn)."""
    mode: str = "none"           # none | blur | box
    y_pct: float = 80.0          # mép trên vùng che (% chiều cao)
    h_pct: float = 12.0          # chiều cao vùng che (%)
    x_pct: float = 5.0           # lề trái/phải vùng che (%) — che giữa, chừa 2 bên
    blur: int = 30
    box_color: str = "#000000"
    box_opacity: int = 85


@dataclass
class ExportOpts:
    encoder: str = "x264"        # x264 | nvenc
    crf: int = 20                # chất lượng (thấp = đẹp hơn, nặng hơn)
    preset: str = "veryfast"
    audio_copy: bool = True


# ------------------------------------------------------------------ tiện ích ffmpeg
def _run(args, cwd=None):
    flags = 0x08000000 if sys.platform == "win32" else 0
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=cwd, creationflags=flags)


def probe(video: str) -> dict:
    """Kích thước, độ dài, fps của video."""
    r = _run([ffmpeg_exe(), "-hide_banner", "-i", video])
    err = r.stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not m:
        raise RuntimeError("Không đọc được video (không thấy luồng hình)")
    w, h = int(m.group(1)), int(m.group(2))
    d = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    f = re.search(r"(\d+(?:\.\d+)?) fps", err)
    rot = re.search(r"rotation of (-?\d+)", err) or re.search(r"rotate\s*:\s*(-?\d+)", err)
    if rot and abs(int(float(rot.group(1)))) % 180 == 90:
        w, h = h, w  # video quay dọc
    return {"w": w, "h": h, "duration": dur, "fps": float(f.group(1)) if f else 0.0,
            "has_audio": "Audio:" in err}


def _read_names(path: str) -> list[tuple[str, str]]:
    """Đọc (họ font, kiểu) từ bảng 'name' của file TTF/OTF/TTC — không cần thư viện ngoài."""
    import struct
    out = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out
    offsets = [0]
    if data[:4] == b"ttcf":
        n = struct.unpack(">I", data[8:12])[0]
        offsets = list(struct.unpack(f">{n}I", data[12:12 + 4 * n]))
    for off in offsets:
        try:
            num = struct.unpack(">H", data[off + 4:off + 6])[0]
            name_off = None
            for i in range(num):
                rec = off + 12 + 16 * i
                if data[rec:rec + 4] == b"name":
                    name_off = struct.unpack(">I", data[rec + 8:rec + 12])[0]
                    break
            if name_off is None:
                continue
            _, count, str_off = struct.unpack(">HHH", data[name_off:name_off + 6])
            names: dict[tuple[int, int], str] = {}
            for i in range(count):
                r = name_off + 6 + 12 * i
                pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", data[r:r + 12])
                if nid not in (1, 2, 16, 17):
                    continue
                raw = data[name_off + str_off + so:name_off + str_off + so + ln]
                if pid in (0, 3):
                    txt = raw.decode("utf-16-be", errors="ignore")
                    prio = 3 if (pid == 3 and lid == 0x409) else 2
                elif pid == 1 and lid == 0:
                    txt, prio = raw.decode("latin-1", errors="ignore"), 1
                else:
                    continue
                if txt and names.get((nid, 0), ("", 0))[1] < prio:
                    names[(nid, 0)] = (txt, prio)
            fam = (names.get((16, 0)) or names.get((1, 0)) or ("", 0))[0]
            sub = (names.get((17, 0)) or names.get((2, 0)) or ("Regular", 0))[0]
            if fam:
                out.append((fam.strip(), sub.strip()))
        except (struct.error, IndexError):
            continue
    return out


def font_catalog() -> dict[str, list[str]]:
    """{họ font: [các file]} từ thư mục font của máy + thư mục fonts của app."""
    cat: dict[str, list[str]] = {}
    seen = set()
    for d in font_dirs():
        for p in d.rglob("*"):
            if p.suffix.lower() not in FONT_EXT or p.name.lower() in seen:
                continue
            seen.add(p.name.lower())
            for fam, _sub in _read_names(str(p)):
                lst = cat.setdefault(fam, [])
                if str(p) not in lst:
                    lst.append(str(p))
    return dict(sorted(cat.items(), key=lambda kv: kv[0].lower()))


def font_dirs() -> list[Path]:
    from .gui import app_dir  # thư mục app (cạnh exe)
    dirs = [app_dir() / "fonts"]
    if sys.platform == "win32":
        dirs += [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts"]
    return [d for d in dirs if d.is_dir()]


# ------------------------------------------------------------------ SRT -> ASS
def _ass_color(hex_color: str, opacity: int = 100) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = round(255 * (100 - max(0, min(100, opacity))) / 100)
    return f"&H{a:02X}{b}{g}{r}".upper()


def _ass_time(t: str) -> str:
    hh, mm, rest = t.replace(".", ",").split(":")
    ss, ms = rest.split(",")
    return f"{int(hh)}:{mm}:{ss}.{int(ms[:3].ljust(3, '0')) // 10:02d}"


def build_ass(cues: list[srt.Cue], style: SubStyle, w: int, h: int) -> str:
    size = max(8, round(h * style.size_pct / 100))
    outline = round(h * style.outline_pct / 100, 1)
    shadow = round(h * style.shadow_pct / 100, 1)
    margin_v = round(h * style.margin_pct / 100)
    margin_lr = round(w * 0.06)
    align = 8 if style.align == "top" else 2
    if style.box:
        border_style, outline_c = 3, _ass_color(style.box_color, style.box_opacity)
        back_c = _ass_color(style.box_color, style.box_opacity)
        outline = max(outline, round(size * 0.18, 1))  # độ dày viền = phần đệm của hộp
    else:
        border_style, outline_c = 1, _ass_color(style.outline_color)
        back_c = _ass_color("#000000", 55)
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nScaledBorderAndShadow: yes\nWrapStyle: 0\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style.font_family},{size},{_ass_color(style.text_color)},&H000000FF,"
        f"{outline_c},{back_c},{-1 if style.bold else 0},{-1 if style.italic else 0},0,0,100,100,0,0,"
        f"{border_style},{outline},{shadow},{align},{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for c in cues:
        text = c.text.replace("{", "(").replace("}", ")")
        text = re.sub(r"</?(i|b|u|font)[^>]*>", "", text)
        text = text.replace("\r", "").replace("\n", r"\N")
        lines.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Default,,0,0,0,,{text}")
    return head + "\n".join(lines) + "\n"


# ------------------------------------------------------------------ bộ lọc
def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def filter_graph(info: dict, cover: Cover, with_subs: bool) -> str:
    w, h = info["w"], info["h"]
    parts, cur = [], "[0:v]"
    if cover.mode in ("blur", "box") and cover.h_pct > 0:
        y = _even(h * cover.y_pct / 100)
        ch = min(_even(h * cover.h_pct / 100), h - y) // 2 * 2
        x = _even(w * cover.x_pct / 100)
        cw = max(2, (w - 2 * x) // 2 * 2)
        if ch >= 2:
            if cover.mode == "blur":
                # kênh màu (chroma) chỉ cao 1/2 -> bán kính tối đa ~1/4 vùng che; tách riêng 2 bán kính
                r = max(1, min(cover.blur, ch // 2 - 1, cw // 2 - 1))
                cr = max(1, min(cover.blur, ch // 4 - 1, cw // 4 - 1))
                parts.append(f"{cur}split[base][src]")
                parts.append(f"[src]crop={cw}:{ch}:{x}:{y},"
                             f"boxblur=luma_radius={r}:luma_power=6:chroma_radius={cr}:chroma_power=4[bl]")
                parts.append(f"[base][bl]overlay={x}:{y}[cv]")
            else:
                col = cover.box_color.lstrip("#")
                parts.append(f"{cur}drawbox=x={x}:y={y}:w={cw}:h={ch}:"
                             f"color=0x{col}@{cover.box_opacity / 100:.2f}:t=fill[cv]")
            cur = "[cv]"
    if with_subs:
        parts.append(f"{cur}ass=sub.ass:fontsdir=fonts[v]")
    else:
        parts.append(f"{cur}null[v]")
    return ";".join(parts)


def _prepare(tmp: str, cues, style: SubStyle, info: dict) -> None:
    with open(os.path.join(tmp, "sub.ass"), "w", encoding="utf-8") as f:
        f.write(build_ass(cues, style, info["w"], info["h"]))
    fonts = os.path.join(tmp, "fonts")
    os.makedirs(fonts, exist_ok=True)
    for ff in style.font_files or []:
        if ff and os.path.isfile(ff):
            shutil.copy(ff, fonts)


# ------------------------------------------------------------------ xem trước
def preview_frame(video: str, cues, style: SubStyle, cover: Cover, at: float, out_png: str,
                  info: dict | None = None, max_w: int = 960) -> str:
    """Xuất 1 khung hình tại giây `at` có phụ đề + vùng che -> PNG."""
    info = info or probe(video)
    tmp = tempfile.mkdtemp(prefix="st_prev_")
    try:
        _prepare(tmp, cues, style, info)
        graph = filter_graph(info, cover, with_subs=True)
        scale = f";[v]scale='min({max_w},iw)':-2[o]"
        r = _run([ffmpeg_exe(), "-hide_banner", "-y", "-ss", f"{max(0.0, at):.3f}", "-copyts",
                  "-i", os.path.abspath(video), "-filter_complex", graph + scale, "-map", "[o]",
                  "-frames:v", "1", os.path.abspath(out_png)], cwd=tmp)
        if r.returncode != 0 or not os.path.exists(out_png):
            raise RuntimeError(_ff_err(r.stderr))
        return out_png
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ff_err(stderr: str) -> str:
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    keep = [l for l in lines if re.search(r"error|invalid|no such|not found|cannot|failed", l, re.I)]
    return "ffmpeg lỗi: " + " | ".join((keep or lines)[-3:])[:400]


# ------------------------------------------------------------------ xuất video
class Exporter:
    def __init__(self, video: str, cues, style: SubStyle, cover: Cover, opts: ExportOpts, out: str,
                 progress=lambda pct, speed: None, log=print):
        self.video, self.cues, self.style, self.cover = video, cues, style, cover
        self.opts, self.out, self.progress, self.log = opts, out, progress, log
        self.proc: subprocess.Popen | None = None
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("q")  # dừng êm, file vẫn mở được
                self.proc.stdin.flush()
            except Exception:  # noqa: BLE001
                self.proc.terminate()

    def _vcodec(self) -> list[str]:
        if self.opts.encoder == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(self.opts.crf + 1),
                    "-b:v", "0", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", self.opts.preset, "-crf", str(self.opts.crf),
                "-pix_fmt", "yuv420p"]

    def run(self) -> str:
        info = probe(self.video)
        tmp = tempfile.mkdtemp(prefix="st_render_")
        try:
            _prepare(tmp, self.cues, self.style, info)
            graph = filter_graph(info, self.cover, with_subs=True)
            out_abs = os.path.abspath(self.out)
            Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
            tries = [self.opts.audio_copy, False] if info["has_audio"] else [False]
            for copy_audio in dict.fromkeys(tries):
                audio = (["-map", "0:a:0?", "-c:a", "copy"] if copy_audio else
                         ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"])
                args = [ffmpeg_exe(), "-hide_banner", "-y", "-i", os.path.abspath(self.video),
                        "-filter_complex", graph, "-map", "[v]", *self._vcodec(), *audio,
                        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_abs]
                self.log(f"  ffmpeg: {info['w']}x{info['h']}, {info['duration']:.0f}s, "
                         f"{'NVENC' if self.opts.encoder == 'nvenc' else 'x264 ' + self.opts.preset}"
                         f", âm thanh {'giữ nguyên' if copy_audio else 'nén lại AAC'}")
                code, err = self._exec(args, tmp, info["duration"])
                if self.stopped.is_set():
                    return "stopped"
                if code == 0:
                    return out_abs
                if copy_audio and re.search(r"codec|audio|Could not find tag", err, re.I):
                    self.log("  ⚠ Không giữ nguyên được âm thanh, thử nén lại AAC…")
                    continue
                raise RuntimeError(_ff_err(err))
            raise RuntimeError("Xuất video thất bại")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _exec(self, args, cwd, duration) -> tuple[int, str]:
        flags = 0x08000000 if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", creationflags=flags)
        err_lines: list[str] = []
        t = threading.Thread(target=lambda: err_lines.extend(self.proc.stderr), daemon=True)
        t.start()
        speed = ""
        for line in self.proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "speed":
                speed = v
            elif k == "out_time_us" and v.isdigit() and duration > 0:
                self.progress(min(99.9, int(v) / 1e6 / duration * 100), speed)
            elif k == "progress" and v == "end":
                self.progress(100.0, speed)
        code = self.proc.wait()
        t.join(timeout=2)
        return code, "".join(err_lines)


def style_to_dict(s: SubStyle) -> dict:
    return asdict(s)
