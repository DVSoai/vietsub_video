"""Đọc / ghi / kiểm tra file SRT."""
from __future__ import annotations

import re
from dataclasses import dataclass

TIME_RE = re.compile(
    r"^(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})(.*)$"
)
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


@dataclass
class Cue:
    index: int
    start: str
    end: str
    text: str
    extra: str = ""  # phần sau timecode (vd: toạ độ X1:...)

    def to_block(self, number: int) -> str:
        return f"{number}\n{self.start} --> {self.end}{self.extra}\n{self.text}\n"


def read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "gb18030", "big5", "cp1258", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _norm_time(t: str) -> str:
    t = t.replace(".", ",")
    hms, ms = t.split(",")
    h, m, s = hms.split(":")
    return f"{int(h):02d}:{m}:{s},{ms.ljust(3, '0')[:3]}"


def parse(content: str) -> list[Cue]:
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    lines = content.split("\n")
    cues: list[Cue] = []
    i = 0
    n = len(lines)
    while i < n:
        # tìm dòng timecode
        m = TIME_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start, end, extra = _norm_time(m.group(1)), _norm_time(m.group(2)), m.group(3).rstrip()
        i += 1
        text_lines = []
        while i < n and lines[i].strip() != "":
            # nếu dòng kế là số + dòng sau là timecode -> cue mới thiếu dòng trống
            if (
                lines[i].strip().isdigit()
                and i + 1 < n
                and TIME_RE.match(lines[i + 1].strip())
            ):
                break
            text_lines.append(lines[i].rstrip())
            i += 1
        # bỏ dòng số thứ tự cuối nếu nó thuộc cue sau
        cues.append(Cue(len(cues) + 1, start, end, "\n".join(text_lines), extra))
    return cues


def compose(cues: list[Cue]) -> str:
    return "\n".join(c.to_block(i + 1) for i, c in enumerate(cues))


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def check_format(content: str) -> list[str]:
    """Trả về danh sách lỗi định dạng (rỗng = hợp lệ)."""
    errors: list[str] = []
    content = content.replace("\r\n", "\n").lstrip("﻿")
    blocks = [b for b in re.split(r"\n\s*\n", content.strip()) if b.strip()]
    expected = 1
    prev_end = None
    for bi, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 2:
            errors.append(f"Khối {bi}: thiếu dòng (chỉ có {len(lines)} dòng)")
            continue
        if not lines[0].strip().isdigit():
            errors.append(f"Khối {bi}: dòng đầu không phải số thứ tự: {lines[0][:40]!r}")
        elif int(lines[0]) != expected:
            errors.append(f"Khối {bi}: số thứ tự {lines[0]} (mong đợi {expected})")
        m = TIME_RE.match(lines[1].strip())
        if not m:
            errors.append(f"Khối {bi}: timecode sai: {lines[1][:60]!r}")
        else:
            s, e = _ms(m.group(1)), _ms(m.group(2))
            if e < s:
                errors.append(f"Khối {bi}: thời gian kết thúc trước bắt đầu")
            if prev_end is not None and s < prev_end - 5000:
                errors.append(f"Khối {bi}: timecode lùi so với khối trước")
            prev_end = e
        if len(lines) < 3 or not any(l.strip() for l in lines[2:]):
            errors.append(f"Khối {bi}: không có nội dung phụ đề")
        expected += 1
    return errors


def _ms(t: str) -> int:
    t = t.replace(".", ",")
    hms, ms = t.split(",")
    h, m, s = hms.split(":")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0")[:3])
