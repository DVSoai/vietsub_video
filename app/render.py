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
class EditRegion:
    """Vùng chữ nhật độc lập trên khung hình GỐC (toạ độ % để không phụ thuộc cỡ khung xem trước)."""
    x_pct: float = 10.0
    y_pct: float = 10.0
    w_pct: float = 20.0
    h_pct: float = 10.0
    mode: str = "erase"          # erase (nội suy nền) | blur | box
    blur: int = 20               # độ mờ (bán kính) cho blur
    box_color: str = "#000000"
    box_opacity: int = 100
    start: float = 0.0           # giây trên timeline video gốc
    end: float = 0.0             # 0 = tới hết video
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "EditRegion":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


def region_px(r: EditRegion, w: int, h: int) -> tuple[int, int, int, int] | None:
    """Đổi % -> pixel chẵn, không chạm mép (delogo yêu cầu), None nếu quá nhỏ."""
    x = max(2, min(w - 6, int(w * r.x_pct / 100) // 2 * 2))
    y = max(2, min(h - 6, int(h * r.y_pct / 100) // 2 * 2))
    cw = min(w - x - 2, int(round(w * r.w_pct / 100))) // 2 * 2
    ch = min(h - y - 2, int(round(h * r.h_pct / 100))) // 2 * 2
    if min(cw, ch) < 4:
        return None
    return x, y, cw, ch


def region_filters(cur: str, regions: list[EditRegion], info: dict, parts: list) -> str:
    """Áp các vùng lên khung gốc. Mỗi vùng một nhãn riêng; thời gian theo timeline gốc."""
    w, h = info["w"], info["h"]
    k = 0
    for r in regions or []:
        if not r.enabled or r.mode not in ("blur", "box", "erase"):
            continue
        px = region_px(r, w, h)
        if not px:
            continue
        x, y, cw, ch = px
        en = ""
        if r.start > 0 or r.end > 0:
            end = r.end if r.end > r.start else 1e9
            en = f":enable='between(t,{r.start:.3f},{end:.3f})'"
        dst = f"[rg{k}]"
        if r.mode == "erase":
            parts.append(f"{cur}delogo=x={x}:y={y}:w={cw}:h={ch}{en}{dst}")
        elif r.mode == "box":
            col = (r.box_color or "#000000").lstrip("#")[:6]
            parts.append(f"{cur}drawbox=x={x}:y={y}:w={cw}:h={ch}:color=0x{col}@{r.box_opacity / 100:.2f}:t=fill{en}{dst}")
        else:
            radius = max(1, min(int(r.blur), cw // 2 - 1, ch // 2 - 1))
            chroma = max(1, min(radius, cw // 4 - 1, ch // 4 - 1))
            parts.append(f"{cur}split[rgb{k}][rgs{k}]")
            parts.append(f"[rgs{k}]crop={cw}:{ch}:{x}:{y},boxblur=luma_radius={radius}:chroma_radius={chroma}[rgc{k}]")
            parts.append(f"[rgb{k}][rgc{k}]overlay={x}:{y}{en}{dst}")
        cur = dst
        k += 1
    return cur


@dataclass
class Cover:
    """Che chữ cứng có sẵn trong video (vd: phụ đề tiếng Trung in sẵn)."""
    mode: str = "none"           # none | blur | box | erase (xoá chữ bằng nội suy nền, như CapCut)
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
    audio_file: str = ""         # file tiếng thay thế (vd: tiếng lồng .vi.dub.m4a); rỗng = tiếng gốc


@dataclass
class Look:
    """Chỉnh hình & tiếng kiểu CapCut khi xuất."""
    speed: float = 1.0           # tốc độ video (phụ đề, tiếng tự khớp theo)
    aspect: str = ""             # "" giữ nguyên | "9:16" | "16:9" | "1:1" | "4:5"
    bg: str = "blur"             # nền khi đổi khung: blur (mờ từ chính video) | black
    zoom: int = 100              # phóng to % (cắt giữa)
    flip: bool = False           # lật ngang
    bright: int = 0              # độ sáng -50..50
    contrast: int = 100          # tương phản %
    sat: int = 100               # bão hoà màu %
    sharpen: int = 0             # làm nét 0..100
    logo: str = ""               # ảnh logo .png
    logo_pos: str = "tr"         # tl tr bl br
    logo_size: int = 12          # % chiều ngang khung
    logo_opacity: int = 90
    fade: float = 0.0            # mờ dần đầu/cuối (giây)
    music: str = ""              # nhạc nền thêm (lặp lại cho đủ dài)
    music_vol: int = 20          # %
    voice_vol: int = 100         # % âm lượng tiếng chính
    denoise: bool = False        # giảm ồn tiếng chính
    regions: list[EditRegion] = field(default_factory=list)
    audio_sped: bool = False     # tiếng đã khớp tốc độ sẵn (tiếng lồng) -> không đổi tốc độ tiếng nữa

    def any_audio(self) -> bool:
        return ((abs(self.speed - 1) > 0.005 and not self.audio_sped) or bool(self.music) or self.fade > 0 or self.voice_vol != 100
                or self.denoise)


ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5)}


def frame_size(info: dict, aspect: str) -> tuple[int, int]:
    w, h = info["w"], info["h"]
    if aspect not in ASPECTS:
        return w, h
    a, b = ASPECTS[aspect]
    long_side = 1920 if max(w, h) >= 1600 else 1280
    if a >= b:
        W, H = long_side, long_side * b // a
    else:
        H, W = long_side, long_side * a // b
    return W // 2 * 2, H // 2 * 2


def _atempo_chain(f: float) -> list[str]:
    out = []
    while f > 2.0:
        out.append("atempo=2.0"); f /= 2
    while f < 0.5:
        out.append("atempo=0.5"); f /= 0.5
    out.append(f"atempo={f:.4f}")
    return out


def look_pre(cur: str, info: dict, look: Look | None, parts: list) -> str:
    """Trước khi che chữ/in phụ đề: tốc độ, lật, phóng to, màu."""
    if not look:
        return cur
    f = []
    if abs(look.speed - 1) > 0.005:
        f.append(f"setpts=PTS/{look.speed:.4f}")
    if look.flip:
        f.append("hflip")
    if look.zoom > 100:
        z = look.zoom / 100
        f.append(f"crop=trunc(iw/{z:.4f}/2)*2:trunc(ih/{z:.4f}/2)*2,scale={info['w']}:{info['h']}")
    if look.bright or look.contrast != 100 or look.sat != 100:
        f.append(f"eq=brightness={look.bright / 100:.3f}:contrast={look.contrast / 100:.3f}:"
                 f"saturation={look.sat / 100:.3f}")
    if look.sharpen:
        f.append(f"unsharp=5:5:{look.sharpen / 50:.2f}:5:5:0")
    if not f:
        return cur
    parts.append(f"{cur}{','.join(f)}[pre]")
    return "[pre]"


def look_post(cur: str, info: dict, look: Look | None, parts: list, logo_idx: int | None, duration: float) -> str:
    """Sau khi in phụ đề: đổi khung hình, logo, mờ dần."""
    if not look:
        return cur
    if look.aspect in ASPECTS:
        W, H = frame_size(info, look.aspect)
        if look.bg == "blur":
            parts.append(f"{cur}split[fa][fb]")
            parts.append(f"[fa]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                         f"boxblur=luma_radius=30:luma_power=2:chroma_radius=15:chroma_power=2[bgb]")
            parts.append(f"[fb]scale={W}:{H}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2[fg]")
            parts.append(f"[bgb][fg]overlay=(W-w)/2:(H-h)/2[fr]")
        else:
            parts.append(f"{cur}scale={W}:{H}:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black[fr]")
        cur = "[fr]"
        fw = W
    else:
        fw = info["w"]
    if logo_idx is not None:
        lw = max(16, int(fw * look.logo_size / 100)) // 2 * 2
        m = max(8, int(fw * 0.025))
        x = {"tl": f"{m}", "bl": f"{m}"}.get(look.logo_pos, f"W-w-{m}")
        y = {"tl": f"{m}", "tr": f"{m}"}.get(look.logo_pos, f"H-h-{m}")
        parts.append(f"[{logo_idx}:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={look.logo_opacity / 100:.2f}[lg]")
        parts.append(f"{cur}[lg]overlay={x}:{y}[lgo]")
        cur = "[lgo]"
    if look.fade > 0 and duration > 0:
        d = min(look.fade, duration / 3)
        parts.append(f"{cur}fade=t=in:st=0:d={d:.2f},fade=t=out:st={max(0.0, duration - d):.2f}:d={d:.2f}[fd]")
        cur = "[fd]"
    return cur


def audio_graph(src: str, look: Look, music_idx: int | None, duration: float) -> str:
    """Chuỗi lọc tiếng -> nhãn [a]."""
    f = []
    if look.denoise:
        f.append("afftdn=nf=-25")
    if abs(look.speed - 1) > 0.005 and not look.audio_sped:
        f += _atempo_chain(look.speed)
    if look.voice_vol != 100:
        f.append(f"volume={look.voice_vol / 100:.2f}")
    if look.fade > 0 and duration > 0:
        d = min(look.fade, duration / 3)
        f.append(f"afade=t=in:st=0:d={d:.2f},afade=t=out:st={max(0.0, duration - d):.2f}:d={d:.2f}")
    parts = [f"[{src}]{','.join(f) or 'anull'}[a0]"]
    if music_idx is not None:
        parts.append(f"[{music_idx}:a]volume={look.music_vol / 100:.2f},atrim=0:{duration:.2f}[m]")
        parts.append("[a0][m]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        parts[-1] = parts[-1][:-4] + "[a]"
    return ";".join(parts)


def scale_cues(cues, speed: float):
    """Đổi thời gian phụ đề theo tốc độ video."""
    if abs(speed - 1) < 0.005:
        return cues
    from .srt import Cue

    def sc(t):
        h, m, rest = t.split(":"); sec, ms = rest.split(",")
        v = (int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000) / speed
        ms_ = int(round(v * 1000))
        return f"{ms_ // 3600000:02d}:{ms_ // 60000 % 60:02d}:{ms_ // 1000 % 60:02d},{ms_ % 1000:03d}"
    return [Cue(c.index, sc(c.start), sc(c.end), c.text, getattr(c, "extra", "")) for c in cues]


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


def filter_graph(info: dict, cover: Cover, with_subs: bool, look: Look | None = None,
                 logo_idx: int | None = None, duration: float = 0.0) -> str:
    w, h = info["w"], info["h"]
    parts, cur = [], "[0:v]"
    # 1) che/xoá chữ cứng trên khung hình GỐC trước -> vị trí đã căn không lệch khi đổi phóng to/lật
    if cover.mode == "erase" and cover.h_pct > 0:
        # delogo: lấp vùng chữ bằng màu nội suy từ viền xung quanh; vùng không được chạm mép khung
        y = max(1, _even(h * cover.y_pct / 100))
        ch = max(2, min(_even(h * cover.h_pct / 100), h - y - 2))
        x = max(1, _even(w * cover.x_pct / 100))
        cw = max(2, min(w - 2 * x, w - x - 2))
        parts.append(f"{cur}delogo=x={x}:y={y}:w={cw}:h={ch}[dl0]")
        # làm mịn nhẹ vết nội suy chỉ trong vùng đã xoá
        r = max(1, min(6, ch // 4 - 1))
        parts.append(f"[dl0]split[dlb][dls]")
        parts.append(f"[dls]crop={cw}:{ch}:{x}:{y},boxblur=luma_radius={r}:chroma_radius={max(1, r // 2)}[dlc]")
        parts.append(f"[dlb][dlc]overlay={x}:{y}[cv]")
        cur = "[cv]"
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
    # 1b) vùng chỉnh sửa riêng: cũng trên khung GỐC, trước tốc độ/lật/phóng to
    cur = region_filters(cur, look.regions if look else [], info, parts)
    # 2) tốc độ, lật, phóng to, màu  3) phụ đề Việt  4) khung hình, logo, mờ dần
    cur = look_pre(cur, info, look, parts)
    if with_subs:
        parts.append(f"{cur}ass=sub.ass:fontsdir=fonts[sb]")
        cur = "[sb]"
    cur = look_post(cur, info, look, parts, logo_idx, duration)
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
                  info: dict | None = None, max_w: int = 960, look: Look | None = None) -> str:
    """Xuất 1 khung hình tại giây `at` có phụ đề + vùng che (+ khung/màu/logo) -> PNG."""
    info = info or probe(video)
    tmp = tempfile.mkdtemp(prefix="st_prev_")
    try:
        _prepare(tmp, cues, style, info)
        lk = None
        extra = []
        if look:
            from dataclasses import replace
            lk = replace(look, speed=1.0, fade=0.0)   # xem trước 1 khung: bỏ tốc độ & mờ dần
            if lk.logo and os.path.exists(lk.logo):
                extra = ["-i", os.path.abspath(lk.logo)]
            else:
                lk.logo = ""
        graph = filter_graph(info, cover, with_subs=True, look=lk, logo_idx=1 if extra else None)
        scale = f";[v]scale='min({max_w},iw)':-2[o]"
        r = _run([ffmpeg_exe(), "-hide_banner", "-y", "-ss", f"{max(0.0, at):.3f}", "-copyts",
                  "-i", os.path.abspath(video), *extra, "-filter_complex", graph + scale, "-map", "[o]",
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
                 progress=lambda pct, speed: None, log=print, look: Look | None = None):
        self.video, self.cues, self.style, self.cover = video, cues, style, cover
        self.look = look or Look()
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
            lk = self.look
            dur = info["duration"] / lk.speed if lk.speed else info["duration"]
            _prepare(tmp, scale_cues(self.cues, lk.speed), self.style, info)
            ext_audio = bool(self.opts.audio_file and os.path.exists(self.opts.audio_file))
            n_in = 2 if ext_audio else 1
            logo_idx = music_idx = None
            look_in = []
            if lk.logo and os.path.exists(lk.logo):
                logo_idx = n_in; n_in += 1; look_in += ["-i", os.path.abspath(lk.logo)]
            if lk.music and os.path.exists(lk.music):
                music_idx = n_in; n_in += 1; look_in += ["-stream_loop", "-1", "-i", os.path.abspath(lk.music)]
            graph = filter_graph(info, self.cover, with_subs=True, look=lk, logo_idx=logo_idx, duration=dur)
            has_a = info["has_audio"] or ext_audio
            if has_a and lk.any_audio():
                graph += ";" + audio_graph("1:a:0" if ext_audio else "0:a:0", lk, music_idx, dur)
            out_abs = os.path.abspath(self.out)
            Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
            filtered_a = has_a and lk.any_audio()
            tries = [self.opts.audio_copy and not filtered_a, False] if has_a else [False]
            for copy_audio in dict.fromkeys(tries):
                src = "[a]" if filtered_a else ("1:a:0" if ext_audio else "0:a:0?")
                audio = (["-map", src, "-c:a", "copy"] if copy_audio else
                         ["-map", src, "-c:a", "aac", "-b:a", "192k"])
                extra_in = ["-i", os.path.abspath(self.opts.audio_file)] if ext_audio else []
                args = [ffmpeg_exe(), "-hide_banner", "-y", "-i", os.path.abspath(self.video), *extra_in, *look_in,
                        "-filter_complex", graph, "-map", "[v]", *self._vcodec(), *audio,
                        *(["-shortest"] if ext_audio else []), "-t", f"{dur:.3f}",
                        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out_abs]
                self.log(f"  ffmpeg: {info['w']}x{info['h']}, {info['duration']:.0f}s, "
                         f"{'NVENC' if self.opts.encoder == 'nvenc' else 'x264 ' + self.opts.preset}"
                         f", âm thanh {'lồng tiếng' if ext_audio else 'gốc'} "
                         f"{'(giữ nguyên)' if copy_audio else '(nén lại AAC)'}")
                code, err = self._exec(args, tmp, dur)
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
