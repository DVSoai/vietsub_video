"""Trang “Xuất video Vietsub”: chọn video + SRT, chỉnh kiểu chữ, che chữ cứng, xem trước, xuất."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path

from PySide6.QtCore import QRectF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from . import srt
from .render import PRESETS, Cover, Exporter, ExportOpts, SubStyle, font_catalog, preview_frame, probe
from .transcribe import MEDIA_EXT, is_media

VIDEO_EXT = {e for e in MEDIA_EXT if e not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}}


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _srt_seconds(t: str) -> float:
    hh, mm, rest = t.replace(".", ",").split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000


# ------------------------------------------------------------------ luồng nền
class _Job(QThread):
    result = Signal(object, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit(None, str(e))


class _ExportThread(QThread):
    progress = Signal(float, str)
    log = Signal(str)
    done = Signal(str, str)

    def __init__(self, exporter: Exporter):
        super().__init__()
        self.ex = exporter
        exporter.progress = lambda p, s: self.progress.emit(p, s)
        exporter.log = self.log.emit

    def run(self):
        try:
            self.done.emit(self.ex.run(), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit("", str(e))


# ------------------------------------------------------------------ ô nhỏ
class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, color="#FFFFFF"):
        super().__init__()
        self.setObjectName("colorBtn")
        self.setFixedSize(46, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, c: str):
        self.color = c.upper()
        self.setStyleSheet(f"QPushButton#colorBtn{{background:{self.color};border:2px solid #36415a;border-radius:8px;}}"
                           f"QPushButton#colorBtn:hover{{border-color:#21c49a;}}")
        self.setToolTip(self.color)

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Chọn màu")
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(self.color)


class PreviewLabel(QLabel):
    """Ảnh xem trước tự co giãn theo khung, vẽ thêm viền vùng che."""

    def __init__(self):
        super().__init__()
        self.setObjectName("preview")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pix: QPixmap | None = None
        self.cover_rect: tuple[float, float, float, float] | None = None  # % x,y,w,h
        self.setText("Chọn video để xem trước")

    def set_image(self, path: str | None):
        self._pix = QPixmap(path) if path else None
        self._redraw()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._redraw()

    def _redraw(self):
        if not self._pix or self._pix.isNull():
            return
        pm = self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self.cover_rect:
            x, y, w, h = self.cover_rect
            p = QPainter(pm)
            pen = QPen(QColor("#21c49a"), 1.5, Qt.DashLine)
            p.setPen(pen)
            p.drawRect(QRectF(pm.width() * x / 100, pm.height() * y / 100,
                              pm.width() * w / 100, pm.height() * h / 100))
            p.end()
        self.setPixmap(pm)


def _card(title: str, step: str | None = None):
    from .gui import card
    return card(title, step)


def _field(label, widget, hint=None):
    from .gui import field
    return field(label, widget, hint)


def _btn(text, kind=None, tip=None):
    from .gui import btn
    return btn(text, kind, tip)


# ------------------------------------------------------------------ trang
class RenderPage(QWidget):
    def __init__(self, cfg: dict, save_cfg, log, out_dir_getter):
        super().__init__()
        self.cfg = cfg
        self.save_cfg = save_cfg
        self.main_log = log
        self.out_dir_getter = out_dir_getter
        self.video = ""
        self.sub_path = ""
        self.info: dict | None = None
        self.cues: list = []
        self.fonts: dict[str, list[str]] = {}
        self.exporter: _ExportThread | None = None
        self._prev_job: _Job | None = None
        self._prev_dirty = False
        self._prev_png = os.path.join(tempfile.gettempdir(), f"st_preview_{os.getpid()}.png")
        self._loading = True
        self._build()
        self._load_state()
        self._loading = False
        QTimer.singleShot(50, self._load_fonts)

    # ============================================================ giao diện
    def _build(self):
        from .gui import DropZone, repolish
        self._repolish = repolish

        # ---- cột trái (cuộn được)
        left = QWidget()
        lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 6, 0); lv.setSpacing(14)

        c1, v1 = _card("Video & phụ đề", "1")
        self.drop = DropZone()
        self.drop.title.setText("Kéo thả video vào đây")
        self.drop.sub.setText("App tự tìm file .vi.srt cùng tên (từ bước Dịch phụ đề)")
        self.ed_sub = QLineEdit(); self.ed_sub.setReadOnly(True)
        self.ed_sub.setPlaceholderText("Chưa có file phụ đề")
        self.btn_sub = _btn("Chọn .srt…", "ghost")
        srow = QHBoxLayout(); srow.setSpacing(8); srow.addWidget(self.ed_sub, 1); srow.addWidget(self.btn_sub)
        v1.addWidget(self.drop)
        v1.addLayout(_field("Phụ đề sẽ in vào video", srow))

        c2, v2 = _card("Kiểu chữ", "2")
        self.cb_preset = QComboBox(); self.cb_preset.addItem("— Chọn kiểu có sẵn —"); self.cb_preset.addItems(PRESETS)
        self.cb_font = QComboBox(); self.cb_font.setEditable(True); self.cb_font.setInsertPolicy(QComboBox.NoInsert)
        self.cb_font.lineEdit().setPlaceholderText("Đang đọc danh sách font…")
        self.sp_size = QDoubleSpinBox(); self.sp_size.setRange(2, 15); self.sp_size.setSingleStep(0.2); self.sp_size.setSuffix(" %")
        self.chk_bold = QCheckBox("Đậm"); self.chk_italic = QCheckBox("Nghiêng")
        self.col_text = ColorButton(); self.col_outline = ColorButton("#000000")
        self.sp_outline = QDoubleSpinBox(); self.sp_outline.setRange(0, 1.5); self.sp_outline.setSingleStep(0.04); self.sp_outline.setSuffix(" %")
        self.sp_shadow = QDoubleSpinBox(); self.sp_shadow.setRange(0, 1.5); self.sp_shadow.setSingleStep(0.05); self.sp_shadow.setSuffix(" %")
        self.chk_box = QCheckBox("Nền hộp sau chữ"); self.col_box = ColorButton("#000000")
        self.sp_box_op = QSpinBox(); self.sp_box_op.setRange(5, 100); self.sp_box_op.setSuffix(" %")
        self.sp_margin = QDoubleSpinBox(); self.sp_margin.setRange(0, 60); self.sp_margin.setSingleStep(0.5); self.sp_margin.setSuffix(" %")
        self.cb_align = QComboBox(); self.cb_align.addItems(["Dưới", "Trên"])

        row_a = QHBoxLayout(); row_a.setSpacing(12)
        row_a.addLayout(_field("Font chữ", self.cb_font), 3)
        row_a.addLayout(_field("Cỡ chữ", self.sp_size), 1)
        bi = QHBoxLayout(); bi.addWidget(self.chk_bold); bi.addWidget(self.chk_italic); bi.addStretch(1)
        row_b = QHBoxLayout(); row_b.setSpacing(12)
        row_b.addLayout(_field("Màu chữ", self.col_text))
        row_b.addLayout(_field("Màu viền", self.col_outline))
        row_b.addLayout(_field("Viền dày", self.sp_outline), 1)
        row_b.addLayout(_field("Bóng đổ", self.sp_shadow), 1)
        row_c = QHBoxLayout(); row_c.setSpacing(12)
        boxcol = QVBoxLayout(); boxcol.addStretch(1); boxcol.addWidget(self.chk_box)
        row_c.addLayout(boxcol, 2)
        row_c.addLayout(_field("Màu nền", self.col_box))
        row_c.addLayout(_field("Độ đậm nền", self.sp_box_op), 1)
        row_d = QHBoxLayout(); row_d.setSpacing(12)
        row_d.addLayout(_field("Vị trí", self.cb_align), 1)
        row_d.addLayout(_field("Cách mép", self.sp_margin, "% chiều cao video"), 1)
        v2.addLayout(_field("Kiểu có sẵn", self.cb_preset))
        v2.addLayout(row_a); v2.addLayout(bi); v2.addLayout(row_b); v2.addLayout(row_c); v2.addLayout(row_d)
        hint = QLabel("Thả thêm font (.ttf/.otf) vào thư mục <b>fonts</b> cạnh app để dùng font kiểu CapCut. "
                      "Nhớ chọn font có đủ dấu tiếng Việt.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        v2.addWidget(hint)

        c3, v3 = _card("Che chữ có sẵn trong video", "3")
        self.cb_cover = QComboBox(); self.cb_cover.addItems(["Không che", "Làm mờ", "Hộp màu"])
        self.sp_cy = QDoubleSpinBox(); self.sp_cy.setRange(0, 99); self.sp_cy.setSingleStep(0.5); self.sp_cy.setSuffix(" %")
        self.sp_ch = QDoubleSpinBox(); self.sp_ch.setRange(1, 100); self.sp_ch.setSingleStep(0.5); self.sp_ch.setSuffix(" %")
        self.sp_cx = QDoubleSpinBox(); self.sp_cx.setRange(0, 45); self.sp_cx.setSingleStep(0.5); self.sp_cx.setSuffix(" %")
        self.sp_blur = QSpinBox(); self.sp_blur.setRange(2, 80)
        self.col_cover = ColorButton("#000000")
        self.sp_cover_op = QSpinBox(); self.sp_cover_op.setRange(10, 100); self.sp_cover_op.setSuffix(" %")
        self.btn_fit = _btn("Đặt phụ đề vào giữa vùng che", "link")
        r1 = QHBoxLayout(); r1.setSpacing(12)
        r1.addLayout(_field("Cách che", self.cb_cover), 2)
        r1.addLayout(_field("Mép trên", self.sp_cy, "% từ trên xuống"), 1)
        r1.addLayout(_field("Cao", self.sp_ch), 1)
        r2 = QHBoxLayout(); r2.setSpacing(12)
        r2.addLayout(_field("Chừa 2 bên", self.sp_cx), 1)
        self.blur_wrap = QWidget(); bw = QHBoxLayout(self.blur_wrap); bw.setContentsMargins(0, 0, 0, 0)
        bw.addLayout(_field("Độ mờ", self.sp_blur))
        self.boxc_wrap = QWidget(); cw = QHBoxLayout(self.boxc_wrap); cw.setContentsMargins(0, 0, 0, 0); cw.setSpacing(12)
        cw.addLayout(_field("Màu hộp", self.col_cover)); cw.addLayout(_field("Độ đậm", self.sp_cover_op))
        r2.addWidget(self.blur_wrap, 1); r2.addWidget(self.boxc_wrap, 2)
        v3.addLayout(r1); v3.addLayout(r2)
        fr = QHBoxLayout(); fr.addWidget(self.btn_fit); fr.addStretch(1); v3.addLayout(fr)
        h3 = QLabel("Khung nét đứt màu xanh trên ảnh xem trước là vùng sẽ bị che. "
                    "Kéo thanh thời gian tới chỗ có chữ Trung để căn cho khít.")
        h3.setObjectName("hint"); h3.setWordWrap(True); v3.addWidget(h3)

        c4, v4 = _card("Xuất video", "4")
        self.cb_enc = QComboBox(); self.cb_enc.addItems(["CPU (x264) — máy nào cũng chạy", "Card NVIDIA (NVENC) — nhanh hơn"])
        self.cb_quality = QComboBox(); self.cb_quality.addItems(["Cao (nặng)", "Vừa (khuyên dùng)", "Nhẹ (file nhỏ)"])
        self.ed_out = QLineEdit(); self.ed_out.setPlaceholderText("Mặc định: cùng thư mục, tên-video.vietsub.mp4")
        self.btn_out = _btn("Chọn…", "ghost")
        orow = QHBoxLayout(); orow.setSpacing(8); orow.addWidget(self.ed_out, 1); orow.addWidget(self.btn_out)
        e1 = QHBoxLayout(); e1.setSpacing(12)
        e1.addLayout(_field("Nén bằng", self.cb_enc), 3); e1.addLayout(_field("Chất lượng", self.cb_quality), 2)
        self.cb_audio = QComboBox(); self.cb_audio.addItems(["Tiếng gốc của video", "Tiếng lồng (giọng Việt)"])
        v4.addLayout(e1)
        v4.addLayout(_field("Âm thanh", self.cb_audio, "Tiếng lồng lấy từ file .vi.dub.m4a do tab Lồng tiếng tạo"))
        v4.addLayout(_field("Lưu thành", orow))

        for c in (c1, c2, c3, c4):
            lv.addWidget(c)
        lv.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(left)
        scroll.setFrameShape(QFrame.NoFrame); scroll.setObjectName("scroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # ---- cột phải: xem trước
        cp, vp = _card("Xem trước", None)
        self.lb_prev_state = QLabel(""); self.lb_prev_state.setObjectName("hint")
        vp.itemAt(0).layout().addWidget(self.lb_prev_state)
        self.preview = PreviewLabel()
        vp.addWidget(self.preview, 1)
        self.sl_time = QSlider(Qt.Horizontal); self.sl_time.setRange(0, 0)
        self.lb_time = QLabel("0:00 / 0:00"); self.lb_time.setObjectName("chip")
        self.btn_prev_cue = _btn("◀ Câu trước", "ghost"); self.btn_next_cue = _btn("Câu sau ▶", "ghost")
        trow = QHBoxLayout(); trow.setSpacing(8)
        trow.addWidget(self.btn_prev_cue); trow.addWidget(self.sl_time, 1); trow.addWidget(self.lb_time); trow.addWidget(self.btn_next_cue)
        vp.addLayout(trow)
        self.lb_cue = QLabel(""); self.lb_cue.setObjectName("hint"); self.lb_cue.setWordWrap(True)
        vp.addWidget(self.lb_cue)

        cols = QHBoxLayout(); cols.setSpacing(14)
        left.adjustSize()
        scroll.setMinimumWidth(max(470, left.minimumSizeHint().width() + 18))
        scroll.setMaximumWidth(560)
        cols.addWidget(scroll, 0); cols.addWidget(cp, 1)

        # ---- thanh xuất
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 14, 18, 14); av.setSpacing(10)
        self.bar = QProgressBar(); self.bar.setFixedHeight(12)
        self.lb_pct = QLabel("0%"); self.lb_pct.setObjectName("cardTitle"); self.lb_pct.setMinimumWidth(48)
        self.lb_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        brow = QHBoxLayout(); brow.addWidget(self.bar, 1); brow.addWidget(self.lb_pct)
        self.lb_state = QLabel("Chọn video và phụ đề, chỉnh kiểu chữ, rồi bấm “Xuất video”.")
        self.lb_state.setObjectName("stage")
        self.btn_open = _btn("Mở thư mục", "ghost")
        self.btn_stop = _btn("■  Dừng", "danger"); self.btn_stop.setEnabled(False)
        self.btn_export = _btn("⬇   Xuất video", "primary")
        crow = QHBoxLayout(); crow.setSpacing(8)
        crow.addWidget(self.lb_state, 1); crow.addWidget(self.btn_open); crow.addWidget(self.btn_stop); crow.addWidget(self.btn_export)
        av.addLayout(brow); av.addLayout(crow)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addLayout(cols, 1); root.addWidget(act)

        # ---- sự kiện
        self.drop.clicked.connect(self._pick_video)
        self.drop.files_dropped.connect(self._on_drop)
        self.btn_sub.clicked.connect(self._pick_sub)
        self.btn_out.clicked.connect(self._pick_out)
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_export.clicked.connect(self._export)
        self.btn_stop.clicked.connect(self._stop)
        self.cb_preset.activated.connect(self._apply_preset)
        self.btn_fit.clicked.connect(self._fit_to_cover)
        self.btn_prev_cue.clicked.connect(lambda: self._jump_cue(-1))
        self.btn_next_cue.clicked.connect(lambda: self._jump_cue(1))
        self.sl_time.valueChanged.connect(self._on_time)
        self.sl_time.sliderReleased.connect(self._schedule_preview)
        for w in (self.sp_size, self.sp_outline, self.sp_shadow, self.sp_box_op, self.sp_margin,
                  self.sp_cy, self.sp_ch, self.sp_cx, self.sp_blur, self.sp_cover_op):
            w.valueChanged.connect(self._changed)
        for w in (self.chk_bold, self.chk_italic, self.chk_box):
            w.toggled.connect(self._changed)
        for w in (self.cb_align, self.cb_cover):
            w.currentIndexChanged.connect(self._changed)
        self.cb_font.currentTextChanged.connect(self._changed)
        for w in (self.col_text, self.col_outline, self.col_box, self.col_cover):
            w.changed.connect(self._changed)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.setInterval(280)
        self._timer.timeout.connect(self._render_preview)

    # ============================================================ trạng thái
    def _style(self) -> SubStyle:
        fam = self.cb_font.currentText().strip()
        return SubStyle(
            font_family=fam, font_files=self.fonts.get(fam, []), size_pct=self.sp_size.value(),
            bold=self.chk_bold.isChecked(), italic=self.chk_italic.isChecked(),
            text_color=self.col_text.color, outline_color=self.col_outline.color,
            outline_pct=self.sp_outline.value(), shadow_pct=self.sp_shadow.value(),
            box=self.chk_box.isChecked(), box_color=self.col_box.color, box_opacity=self.sp_box_op.value(),
            margin_pct=self.sp_margin.value(), align="top" if self.cb_align.currentIndex() == 1 else "bottom")

    def _cover(self) -> Cover:
        return Cover(mode=["none", "blur", "box"][self.cb_cover.currentIndex()],
                     y_pct=self.sp_cy.value(), h_pct=self.sp_ch.value(), x_pct=self.sp_cx.value(),
                     blur=self.sp_blur.value(), box_color=self.col_cover.color,
                     box_opacity=self.sp_cover_op.value())

    def dub_audio(self) -> str:
        if not self.video:
            return ""
        p = Path(self.video)
        f = p.with_name(p.stem + ".vi.dub.m4a")
        return str(f) if f.exists() else ""

    def _opts(self) -> ExportOpts:
        crf = [18, 20, 23][self.cb_quality.currentIndex()]
        audio = self.dub_audio() if self.cb_audio.currentIndex() == 1 else ""
        return ExportOpts(encoder="nvenc" if self.cb_enc.currentIndex() == 1 else "x264", crf=crf, audio_file=audio)

    def _set_style(self, st: SubStyle):
        self._loading = True
        if st.font_family:
            self.cb_font.setCurrentText(st.font_family)
        self.sp_size.setValue(st.size_pct); self.chk_bold.setChecked(st.bold); self.chk_italic.setChecked(st.italic)
        self.col_text.set_color(st.text_color); self.col_outline.set_color(st.outline_color)
        self.sp_outline.setValue(st.outline_pct); self.sp_shadow.setValue(st.shadow_pct)
        self.chk_box.setChecked(st.box); self.col_box.set_color(st.box_color); self.sp_box_op.setValue(st.box_opacity)
        self.sp_margin.setValue(st.margin_pct); self.cb_align.setCurrentIndex(1 if st.align == "top" else 0)
        self._loading = False

    def _load_state(self):
        r = self.cfg.get("render", {})
        names = {f.name for f in fields(SubStyle)}
        st = SubStyle(**{k: v for k, v in r.get("style", {}).items() if k in names})
        if not r.get("style"):
            st.font_family = "Arial" if sys.platform == "win32" else "DejaVu Sans"
        self._set_style(st)
        cnames = {f.name for f in fields(Cover)}
        cv = Cover(**{k: v for k, v in r.get("cover", {}).items() if k in cnames})
        self.cb_cover.setCurrentIndex({"none": 0, "blur": 1, "box": 2}.get(cv.mode, 0))
        self.sp_cy.setValue(cv.y_pct); self.sp_ch.setValue(cv.h_pct); self.sp_cx.setValue(cv.x_pct)
        self.sp_blur.setValue(cv.blur); self.col_cover.set_color(cv.box_color); self.sp_cover_op.setValue(cv.box_opacity)
        self.cb_enc.setCurrentIndex(r.get("encoder", 0)); self.cb_quality.setCurrentIndex(r.get("quality", 1))
        self._update_cover_widgets()

    def store_state(self):
        st = asdict(self._style()); st.pop("font_files", None)
        self.cfg["render"] = {"style": st, "cover": asdict(self._cover()),
                              "encoder": self.cb_enc.currentIndex(), "quality": self.cb_quality.currentIndex()}

    def _changed(self, *_):
        self._update_cover_widgets()
        if self._loading:
            return
        if self.sender() is not self.cb_preset:
            self.cb_preset.setCurrentIndex(0)
        self._schedule_preview()

    def _update_cover_widgets(self):
        m = self.cb_cover.currentIndex()
        self.blur_wrap.setVisible(m == 1)
        self.boxc_wrap.setVisible(m == 2)
        for w in (self.sp_cy, self.sp_ch, self.sp_cx, self.btn_fit):
            w.setEnabled(m != 0)
        self.preview.cover_rect = None if m == 0 else (
            self.sp_cx.value(), self.sp_cy.value(), 100 - 2 * self.sp_cx.value(), self.sp_ch.value())
        self.preview._redraw()

    def _apply_preset(self, idx: int):
        if idx <= 0:
            return
        name = self.cb_preset.itemText(idx)
        cur = self._style()
        base = SubStyle(font_family=cur.font_family, margin_pct=cur.margin_pct, align=cur.align)
        for k, v in PRESETS[name].items():
            setattr(base, k, v)
        self._set_style(base)
        self._schedule_preview()

    def _fit_to_cover(self):
        """Đặt phụ đề nằm giữa vùng che (phụ đề Việt thay đúng chỗ chữ Trung)."""
        center = self.sp_cy.value() + self.sp_ch.value() / 2
        line_h = self.sp_size.value() * 1.25
        self.cb_align.setCurrentIndex(0)
        self.sp_margin.setValue(max(0.0, round(100 - center - line_h / 2, 1)))

    # ============================================================ font
    def _load_fonts(self):
        self.lb_prev_state.setText("đang đọc font…")
        self._font_job = _Job(font_catalog)
        self._font_job.result.connect(self._on_fonts)
        self._font_job.start()

    def _on_fonts(self, cat, err):
        self.fonts = cat or {}
        cur = self.cb_font.currentText()
        self._loading = True
        self.cb_font.clear()
        self.cb_font.addItems(list(self.fonts))
        if cur in self.fonts:
            self.cb_font.setCurrentText(cur)
        elif self.fonts:
            for pref in ("Arial", "Segoe UI", "Roboto", "DejaVu Sans"):
                if pref in self.fonts:
                    self.cb_font.setCurrentText(pref)
                    break
        self._loading = False
        self.lb_prev_state.setText(f"{len(self.fonts)} font" if not err else "không đọc được font")
        self._schedule_preview()

    # ============================================================ chọn file
    def set_video(self, path: str, sub: str | None = None):
        self.video = path
        self.drop.set_files([path])
        self.info = None
        try:
            self.info = probe(path)
        except Exception as e:  # noqa: BLE001
            self._state(f"✖ {e}", err=True)
            return
        dur = self.info["duration"]
        self.sl_time.blockSignals(True)
        self.sl_time.setRange(0, int(dur * 10))
        self.sl_time.setValue(0)
        self.sl_time.blockSignals(False)
        sub = sub or self._guess_sub(path)
        if sub:
            self.set_sub(sub)
        self.ed_out.setText(str(Path(path).with_name(Path(path).stem + ".vietsub.mp4")))
        has_dub = bool(self.dub_audio())
        self.cb_audio.model().item(1).setEnabled(has_dub)
        self.cb_audio.setCurrentIndex(1 if has_dub else 0)
        self._state(f"Video {self.info['w']}×{self.info['h']}, dài {_fmt(dur)}"
                    + ("" if self.info["has_audio"] else " (không có tiếng)"))
        if self.cues:
            self.sl_time.setValue(int(_srt_seconds(self.cues[0].start) * 10 + 5))
        self._on_time(self.sl_time.value())
        self._schedule_preview()

    def _guess_sub(self, video: str) -> str:
        p = Path(video)
        dirs = [p.parent]
        od = self.out_dir_getter()
        if od:
            dirs.insert(0, Path(od))
        for d in dirs:
            for name in (f"{p.stem}.vi.srt", f"{p.stem}.srt"):
                if (d / name).exists():
                    return str(d / name)
        return ""

    def set_sub(self, path: str):
        try:
            cues = srt.parse(srt.read_text(path))
        except OSError as e:
            self._state(f"✖ Không đọc được phụ đề: {e}", err=True)
            return
        self.sub_path, self.cues = path, cues
        self.ed_sub.setText(path)
        self._state(f"Phụ đề: {len(cues)} câu — {Path(path).name}")
        self._schedule_preview()

    def _on_drop(self, files):
        vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXT]
        subs = [f for f in files if f.lower().endswith(".srt")]
        if vids:
            self.set_video(vids[0], subs[0] if subs else None)
        elif subs:
            self.set_sub(subs[0])

    def _pick_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn video", self.cfg.get("last_dir", ""),
                                           "Video (" + " ".join("*" + x for x in sorted(VIDEO_EXT)) + ");;Tất cả (*.*)")
        if f:
            self.set_video(f)

    def _pick_sub(self):
        start = str(Path(self.video).parent) if self.video else ""
        f, _ = QFileDialog.getOpenFileName(self, "Chọn phụ đề", start, "Phụ đề (*.srt)")
        if f:
            self.set_sub(f)

    def _pick_out(self):
        f, _ = QFileDialog.getSaveFileName(self, "Lưu video", self.ed_out.text(), "Video MP4 (*.mp4)")
        if f:
            self.ed_out.setText(f if f.lower().endswith(".mp4") else f + ".mp4")

    def _open_folder(self):
        target = self.ed_out.text() or self.video
        if not target:
            return
        folder = str(Path(target).parent)
        if sys.platform == "win32":
            if os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    # ============================================================ xem trước
    def _cur_time(self) -> float:
        return self.sl_time.value() / 10

    def _on_time(self, v):
        t = v / 10
        dur = self.info["duration"] if self.info else 0
        self.lb_time.setText(f"{_fmt(t)} / {_fmt(dur)}")
        cue = next((c for c in self.cues if _srt_seconds(c.start) <= t <= _srt_seconds(c.end)), None)
        self.lb_cue.setText(f"Câu {cue.index}: {cue.text.replace(chr(10), ' / ')}" if cue else "(không có phụ đề ở thời điểm này)")
        if not self.sl_time.isSliderDown():
            self._schedule_preview()

    def _jump_cue(self, step: int):
        if not self.cues:
            return
        t = self._cur_time()
        starts = [_srt_seconds(c.start) for c in self.cues]
        if step > 0:
            nxt = next((s for s in starts if s > t + 0.05), None)
        else:
            prev = [s for s in starts if s < t - 0.6]
            nxt = prev[-1] if prev else None
        if nxt is not None:
            self.sl_time.setValue(int(nxt * 10 + 5))

    def _schedule_preview(self):
        if self._loading or not self.video or not self.info:
            return
        self._timer.start()

    def _render_preview(self):
        if self._prev_job and self._prev_job.isRunning():
            self._prev_dirty = True
            return
        self._prev_dirty = False
        video, cues, st, cv, at, info = self.video, list(self.cues), self._style(), self._cover(), self._cur_time(), self.info
        out = self._prev_png
        self.lb_prev_state.setText("đang vẽ…")
        self._prev_job = _Job(lambda: preview_frame(video, cues, st, cv, at, out, info, max_w=1280))
        self._prev_job.result.connect(self._on_preview)
        self._prev_job.start()

    def _on_preview(self, path, err):
        if err:
            self.lb_prev_state.setText("lỗi xem trước")
            self._state(f"✖ Xem trước lỗi: {err}", err=True)
        else:
            self.preview.set_image(path)
            self.lb_prev_state.setText(f"{_fmt(self._cur_time())}")
        if self._prev_dirty:
            self._render_preview()

    # ============================================================ xuất
    def _state(self, text: str, err: bool = False):
        self.lb_state.setText(text)
        self.lb_state.setStyleSheet("color:#ef5d6c" if err else "")

    def _export(self):
        if not self.video or not self.info:
            return QMessageBox.warning(self, "Thiếu video", "Chưa chọn video")
        if not self.cues:
            return QMessageBox.warning(self, "Thiếu phụ đề", "Chưa chọn file phụ đề .srt")
        out = self.ed_out.text().strip() or str(Path(self.video).with_name(Path(self.video).stem + ".vietsub.mp4"))
        if os.path.abspath(out) == os.path.abspath(self.video):
            return QMessageBox.warning(self, "Trùng tên", "File xuất không được trùng với video gốc")
        if os.path.exists(out) and QMessageBox.question(
                self, "Ghi đè?", f"{Path(out).name} đã có. Ghi đè?") != QMessageBox.Yes:
            return
        self.store_state(); self.save_cfg()
        ex = Exporter(self.video, list(self.cues), self._style(), self._cover(), self._opts(), out)
        self.exporter = _ExportThread(ex)
        self.exporter.progress.connect(self._on_progress)
        self.exporter.log.connect(self.main_log)
        self.exporter.done.connect(self._on_done)
        self._t0 = time.monotonic()
        self._running(True)
        self._state("Đang xuất video…")
        self.main_log(f"━━ Xuất video: {Path(self.video).name} + {Path(self.sub_path).name} → {out}")
        self.exporter.start()

    def _on_progress(self, pct: float, speed: str):
        self.bar.setValue(int(pct))
        self.lb_pct.setText(f"{pct:.0f}%")
        el = time.monotonic() - self._t0
        eta = el * (100 - pct) / pct if pct > 1 else 0
        self._state(f"Đang in phụ đề vào video · tốc độ {speed or '…'} · đã chạy {_fmt(el)}"
                    + (f" · còn khoảng {_fmt(eta)}" if eta else ""))

    def _on_done(self, out: str, err: str):
        self._running(False)
        if err:
            self._state(f"✖ {err}", err=True)
            self.main_log(f"  ✖ {err}")
            if "nvenc" in err.lower() or "cuda" in err.lower():
                self._state("✖ Máy không dùng được NVENC (card NVIDIA). Chọn “CPU (x264)” rồi xuất lại.", err=True)
        elif out == "stopped":
            self._state("Đã dừng xuất video.")
            self.main_log("  ■ Đã dừng xuất video")
        else:
            self.bar.setValue(100); self.lb_pct.setText("100%")
            self._state(f"✔ Xong sau {_fmt(time.monotonic() - self._t0)} → {out}")
            self.main_log(f"  ✔ Đã xuất {out}")

    def _stop(self):
        if self.exporter:
            self.exporter.ex.stop()
            self._state("Đang dừng…")

    def _running(self, on: bool):
        self.btn_export.setEnabled(not on)
        self.btn_stop.setEnabled(on)
        self.drop.setEnabled(not on)
        if on:
            self.bar.setValue(0); self.lb_pct.setText("0%")

    def busy(self) -> bool:
        return bool(self.exporter and self.exporter.isRunning())

    def shutdown(self):
        if self.busy():
            self.exporter.ex.stop()
            self.exporter.wait(5000)
        self.store_state()
