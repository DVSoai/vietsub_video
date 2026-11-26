"""Hộp thoại “Chỉnh sửa vùng hình ảnh”: kéo chuột trên khung hình video để tạo/di chuyển/đổi cỡ
các vùng, chọn cách xử lý (nội suy nền / làm mờ / che), khoảng thời gian, xem trước kết quả thật.

Toạ độ lưu theo % khung hình GỐC (đã loại phần viền đen khi ảnh được co vào khung widget)."""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import theme
from .render import Cover, EditRegion, Look, SubStyle, preview_frame, probe
from .transcribe import ffmpeg_exe

MODES = [("Nội suy nền (xoá)", "erase"), ("Làm mờ", "blur"), ("Che bằng màu", "box")]
HANDLE = 9          # px: ô kéo đổi cỡ ở góc


def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


class _Job(QThread):
    done = Signal(str, str, int)        # đường dẫn ảnh, lỗi, mã yêu cầu

    def __init__(self, fn, tag: int):
        super().__init__()
        self.fn, self.tag = fn, tag

    def run(self):
        try:
            self.done.emit(self.fn(), "", self.tag)
        except Exception as e:  # noqa: BLE001
            self.done.emit("", str(e)[:300], self.tag)


# ====================================================================== khung vẽ
class RegionCanvas(QWidget):
    changed = Signal()                  # vùng bị kéo / đổi cỡ / tạo mới
    selected = Signal(int)

    def __init__(self, regions: list[EditRegion]):
        super().__init__()
        self.regions = regions
        self.sel = -1
        self.pix: QPixmap | None = None
        self.show_boxes = True
        self._drag = None               # (kiểu, chỉ số, điểm bắt đầu %, vùng ban đầu)
        self.setMouseTracking(True)
        self.setMinimumSize(560, 315)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # --- quy đổi toạ độ (bỏ viền đen do giữ tỉ lệ)
    def _img_rect(self) -> QRectF:
        if not self.pix or self.pix.isNull():
            return QRectF(0, 0, self.width(), self.height())
        pw, ph = self.pix.width(), self.pix.height()
        k = min(self.width() / pw, self.height() / ph)
        w, h = pw * k, ph * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_pct(self, p: QPointF) -> tuple[float, float]:
        r = self._img_rect()
        x = (p.x() - r.x()) / r.width() * 100
        y = (p.y() - r.y()) / r.height() * 100
        return min(100.0, max(0.0, x)), min(100.0, max(0.0, y))

    def _rect_of(self, g: EditRegion) -> QRectF:
        r = self._img_rect()
        return QRectF(r.x() + r.width() * g.x_pct / 100, r.y() + r.height() * g.y_pct / 100,
                      r.width() * g.w_pct / 100, r.height() * g.h_pct / 100)

    def _hit(self, pos: QPointF):
        for i in range(len(self.regions) - 1, -1, -1):
            rr = self._rect_of(self.regions[i])
            corner = QRectF(rr.right() - HANDLE, rr.bottom() - HANDLE, HANDLE * 2, HANDLE * 2)
            if corner.contains(pos):
                return "resize", i
            if rr.contains(pos):
                return "move", i
        return None, -1

    # --- chuột
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        kind, i = self._hit(pos)
        x, y = self._to_pct(pos)
        if kind is None:                 # kéo trên chỗ trống -> vùng mới
            g = EditRegion(x, y, 0.5, 0.5)
            self.regions.append(g)
            i, kind = len(self.regions) - 1, "resize"
        self.sel = i
        g = self.regions[i]
        self._drag = (kind, i, (x, y), replace(g))
        self.selected.emit(i)
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            kind, _ = self._hit(pos)
            self.setCursor(Qt.SizeFDiagCursor if kind == "resize" else
                           Qt.SizeAllCursor if kind == "move" else Qt.CrossCursor)
            return
        kind, i, (sx, sy), g0 = self._drag
        x, y = self._to_pct(pos)
        g = self.regions[i]
        if kind == "move":
            g.x_pct = min(100 - g0.w_pct, max(0.0, g0.x_pct + x - sx))
            g.y_pct = min(100 - g0.h_pct, max(0.0, g0.y_pct + y - sy))
        else:
            g.w_pct = max(0.5, min(100 - g.x_pct, x - g.x_pct))
            g.h_pct = max(0.5, min(100 - g.y_pct, y - g.y_pct))
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            kind, i, _, _ = self._drag
            g = self.regions[i]
            if g.w_pct < 1 or g.h_pct < 1:          # bấm nhầm: vùng quá nhỏ -> bỏ
                del self.regions[i]
                self.sel = -1
                self.selected.emit(-1)
            self._drag = None
            self.changed.emit()
            self.update()

    # --- vẽ
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#05070a"))
        r = self._img_rect()
        if self.pix and not self.pix.isNull():
            p.drawPixmap(r.toRect(), self.pix)
        else:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Đang lấy khung hình…")
        if not self.show_boxes:
            return
        for i, g in enumerate(self.regions):
            rr = self._rect_of(g)
            col = QColor(theme.ACCENT if i == self.sel else theme.WARN)
            if not g.enabled:
                col = QColor(theme.MUTED)
            p.setPen(QPen(col, 2 if i == self.sel else 1.4, Qt.SolidLine if i == self.sel else Qt.DashLine))
            fill = QColor(col); fill.setAlpha(40)
            p.setBrush(fill)
            p.drawRect(rr)
            p.setBrush(col)
            p.drawRect(QRectF(rr.right() - HANDLE / 2, rr.bottom() - HANDLE / 2, HANDLE, HANDLE))
            p.setPen(QColor("#ffffff"))
            p.drawText(rr.adjusted(4, 2, 0, 0), Qt.AlignLeft | Qt.AlignTop, str(i + 1))


# ====================================================================== hộp thoại
class RegionDialog(QDialog):
    def __init__(self, video: str, regions: list[EditRegion], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chỉnh sửa vùng hình ảnh")
        self.video = video
        self.regions = [replace(r) for r in regions]
        self.info = probe(video)
        self.dur = float(self.info.get("duration") or 0)
        self._tmp = tempfile.mkdtemp(prefix="st_region_")
        self._tag = 0
        self._jobs: list[_Job] = []
        self._loading = False

        # --- khung hình + thời gian
        self.canvas = RegionCanvas(self.regions)
        self.slider = QSlider(Qt.Horizontal); self.slider.setRange(0, max(1, int(self.dur * 10)))
        self.lb_time = QLabel("0:00.0"); self.lb_time.setObjectName("chip")
        self.chk_after = QCheckBox("Xem kết quả sau xử lý")
        self.lb_state = QLabel(""); self.lb_state.setObjectName("hint")
        tl = QHBoxLayout(); tl.addWidget(self.slider, 1); tl.addWidget(self.lb_time)
        bl = QHBoxLayout(); bl.addWidget(self.chk_after); bl.addStretch(1); bl.addWidget(self.lb_state)
        hint = QLabel("Kéo chuột trên khung hình để tạo vùng · kéo vùng để di chuyển · kéo ô vuông ở góc dưới phải "
                      "để đổi cỡ · tua thanh thời gian để xem khung khác.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        left = QVBoxLayout(); left.addWidget(self.canvas, 1); left.addLayout(tl); left.addLayout(bl); left.addWidget(hint)

        # --- danh sách + thuộc tính
        self.lst = QListWidget(); self.lst.setObjectName("partList"); self.lst.setFixedHeight(150)
        self.btn_add = QPushButton("Thêm vùng"); self.btn_del = QPushButton("Xoá vùng")
        self.btn_add.setObjectName("ghost"); self.btn_del.setObjectName("ghost")
        ab = QHBoxLayout(); ab.addWidget(self.btn_add); ab.addWidget(self.btn_del)
        self.chk_on = QCheckBox("Bật vùng này")
        self.cb_mode = QComboBox()
        for label, m in MODES:
            self.cb_mode.addItem(label, m)
        self.sp_blur = QSpinBox(); self.sp_blur.setRange(2, 60)
        self.btn_color = QPushButton("Màu"); self.btn_color.setObjectName("ghost")
        self.sp_op = QSpinBox(); self.sp_op.setRange(10, 100); self.sp_op.setSuffix(" %"); self.sp_op.setFixedWidth(78)
        self.sp_start = QDoubleSpinBox(); self.sp_end = QDoubleSpinBox()
        for sp in (self.sp_start, self.sp_end):
            sp.setRange(0, max(0.1, self.dur)); sp.setDecimals(1); sp.setSuffix(" s")
        self.sp_end.setSpecialValueText("hết video")
        self.btn_from_now = QPushButton("← lúc này"); self.btn_to_now = QPushButton("← lúc này")
        for b in (self.btn_from_now, self.btn_to_now):
            b.setObjectName("link")
        self.sp_xywh = []
        for _ in range(4):
            sp = QDoubleSpinBox(); sp.setRange(0, 100); sp.setDecimals(1); sp.setSuffix(" %")
            self.sp_xywh.append(sp)

        def row(label, *ws):
            h = QHBoxLayout(); lb = QLabel(label); lb.setObjectName("fieldLabel"); lb.setFixedWidth(78)
            h.addWidget(lb)
            for x in ws:
                h.addWidget(x, 1 if not isinstance(x, QPushButton) else 0)
            return h
        self.props = QWidget(); pv = QVBoxLayout(self.props); pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(8)
        pv.addWidget(self.chk_on)
        pv.addLayout(row("Cách xử lý", self.cb_mode))
        self.row_blur = QWidget(); rb = QVBoxLayout(self.row_blur); rb.setContentsMargins(0, 0, 0, 0)
        rb.addLayout(row("Độ mờ", self.sp_blur))
        self.row_box = QWidget(); rx = QVBoxLayout(self.row_box); rx.setContentsMargins(0, 0, 0, 0)
        rx.addLayout(row("Màu che", self.btn_color, self.sp_op))
        pv.addWidget(self.row_blur); pv.addWidget(self.row_box)
        pv.addLayout(row("Từ", self.sp_start, self.btn_from_now))
        pv.addLayout(row("Đến", self.sp_end, self.btn_to_now))
        pv.addLayout(row("Vị trí X/Y", self.sp_xywh[0], self.sp_xywh[1]))
        pv.addLayout(row("Rộng/Cao", self.sp_xywh[2], self.sp_xywh[3]))
        right = QVBoxLayout(); right.setSpacing(10)
        t = QLabel("Các vùng"); t.setObjectName("cardTitle"); right.addWidget(t)
        right.addWidget(self.lst); right.addLayout(ab); right.addWidget(self.props); right.addStretch(1)
        note = QLabel("Chỉ dùng cho video của bạn hoặc video bạn được phép chỉnh sửa.")
        note.setObjectName("hint"); note.setWordWrap(True); right.addWidget(note)
        rw = QWidget(); rw.setLayout(right); rw.setFixedWidth(300)

        body = QHBoxLayout(); body.setSpacing(16); body.addLayout(left, 1); body.addWidget(rw)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Lưu vùng"); btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.button(QDialogButtonBox.Cancel).setText("Huỷ")
        root = QVBoxLayout(self); root.setContentsMargins(18, 16, 18, 16); root.setSpacing(12)
        root.addLayout(body, 1); root.addWidget(btns)
        self.resize(1080, 620)

        # --- sự kiện
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject)
        self._frame_timer = QTimer(self); self._frame_timer.setSingleShot(True); self._frame_timer.timeout.connect(self._refresh)
        self.slider.valueChanged.connect(self._on_time)
        self.chk_after.toggled.connect(lambda _: self._refresh())
        self.canvas.changed.connect(self._on_canvas)
        self.canvas.selected.connect(self._select)
        self.lst.currentRowChanged.connect(self._list_select)
        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._delete)
        self.btn_color.clicked.connect(self._pick_color)
        self.btn_from_now.clicked.connect(lambda: self.sp_start.setValue(self._now()))
        self.btn_to_now.clicked.connect(lambda: self.sp_end.setValue(self._now()))
        for w in (self.sp_blur, self.sp_op, self.sp_start, self.sp_end, *self.sp_xywh):
            w.valueChanged.connect(self._props_changed)
        self.cb_mode.currentIndexChanged.connect(self._props_changed)
        self.chk_on.toggled.connect(self._props_changed)

        self._fill_list()
        self._select(0 if self.regions else -1)
        self.slider.setValue(min(self.slider.maximum(), 50))
        self._refresh()

    # ------------------------------------------------------------ thời gian & ảnh
    def _now(self) -> float:
        return self.slider.value() / 10

    def _on_time(self, _):
        self.lb_time.setText(_fmt(self._now()))
        self._frame_timer.start(180)

    def _refresh(self):
        self._tag += 1
        tag, t = self._tag, self._now()
        out = os.path.join(self._tmp, f"f{tag}.png")
        if self.chk_after.isChecked():
            regs = [replace(r) for r in self.regions]
            info = self.info
            fn = lambda: preview_frame(self.video, [], SubStyle(), Cover(), t, out, info, max_w=1280,  # noqa: E731
                                       look=Look(regions=regs))
            self.lb_state.setText("đang xử lý…")
        else:
            fn = lambda: self._raw_frame(t, out)  # noqa: E731
            self.lb_state.setText("")
        job = _Job(fn, tag)
        job.done.connect(self._on_frame)
        self._jobs.append(job)
        job.start()

    def _raw_frame(self, t: float, out: str) -> str:
        r = subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", self.video, "-frames:v", "1",
                            "-vf", "scale='min(1280,iw)':-2", out], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-200:] or "không lấy được khung hình")
        return out

    def _on_frame(self, path, err, tag):
        self._jobs = [j for j in self._jobs if j.isRunning()]
        if tag != self._tag:
            return
        if err:
            self.lb_state.setText(f"✖ {err}")
            return
        self.canvas.pix = QPixmap(path)
        self.canvas.show_boxes = not self.chk_after.isChecked()
        self.lb_state.setText("✔ kết quả thật (giống video xuất)" if self.chk_after.isChecked() else "")
        self.canvas.update()

    # ------------------------------------------------------------ danh sách
    def _label(self, i: int, g: EditRegion) -> str:
        mode = dict((m, l) for l, m in MODES)[g.mode].split(" (")[0]
        tt = "cả video" if not (g.start or g.end) else f"{_fmt(g.start)}–{_fmt(g.end) if g.end > g.start else 'hết'}"
        return f"{i + 1}. {mode} · {tt}" + ("" if g.enabled else " · tắt")

    def _fill_list(self):
        self.lst.blockSignals(True)
        cur = self.canvas.sel
        self.lst.clear()
        for i, g in enumerate(self.regions):
            self.lst.addItem(QListWidgetItem(self._label(i, g)))
        self.lst.setCurrentRow(cur)
        self.lst.blockSignals(False)

    def _list_select(self, i):
        self.canvas.sel = i
        self.canvas.update()
        self._load_props(i)

    def _select(self, i):
        self.canvas.sel = i
        self._fill_list()
        self._load_props(i)
        self.canvas.update()

    def _add(self):
        self.regions.append(EditRegion(40, 40, 20, 12))
        self._select(len(self.regions) - 1)
        self._after_edit()

    def _delete(self):
        i = self.canvas.sel
        if 0 <= i < len(self.regions):
            del self.regions[i]
            self._select(min(i, len(self.regions) - 1))
            self._after_edit()

    def _on_canvas(self):
        self._fill_list()
        self._load_props(self.canvas.sel)
        if self.chk_after.isChecked():
            self._frame_timer.start(300)

    def _after_edit(self):
        self._fill_list()
        if self.chk_after.isChecked():
            self._frame_timer.start(300)
        self.canvas.update()

    # ------------------------------------------------------------ thuộc tính
    def _load_props(self, i):
        ok = 0 <= i < len(self.regions)
        self.props.setEnabled(ok)
        self.btn_del.setEnabled(ok)
        if not ok:
            return
        g = self.regions[i]
        self._loading = True
        self.chk_on.setChecked(g.enabled)
        self.cb_mode.setCurrentIndex(max(0, self.cb_mode.findData(g.mode)))
        self.sp_blur.setValue(int(g.blur)); self.sp_op.setValue(int(g.box_opacity))
        self.sp_start.setValue(g.start); self.sp_end.setValue(g.end)
        for sp, v in zip(self.sp_xywh, (g.x_pct, g.y_pct, g.w_pct, g.h_pct)):
            sp.setValue(v)
        self._color_btn(g.box_color)
        self.row_blur.setVisible(g.mode == "blur"); self.row_box.setVisible(g.mode == "box")
        self._loading = False

    def _props_changed(self, *_):
        i = self.canvas.sel
        if self._loading or not (0 <= i < len(self.regions)):
            return
        g = self.regions[i]
        g.enabled = self.chk_on.isChecked()
        g.mode = self.cb_mode.currentData()
        g.blur = self.sp_blur.value(); g.box_opacity = self.sp_op.value()
        g.start, g.end = self.sp_start.value(), self.sp_end.value()
        x, y, w, h = (sp.value() for sp in self.sp_xywh)
        g.x_pct, g.y_pct = min(x, 99.0), min(y, 99.0)
        g.w_pct, g.h_pct = max(0.5, min(w, 100 - g.x_pct)), max(0.5, min(h, 100 - g.y_pct))
        self.row_blur.setVisible(g.mode == "blur"); self.row_box.setVisible(g.mode == "box")
        self._after_edit()

    def _color_btn(self, c: str):
        from PySide6.QtGui import QIcon
        pm = QPixmap(16, 16); pm.fill(QColor(c))
        self.btn_color.setIcon(QIcon(pm))
        self.btn_color.setText(c.upper())

    def _pick_color(self):
        i = self.canvas.sel
        if not (0 <= i < len(self.regions)):
            return
        c = QColorDialog.getColor(QColor(self.regions[i].box_color), self, "Màu che")
        if c.isValid():
            self.regions[i].box_color = c.name()
            self._color_btn(c.name())
            self._after_edit()

    def result_regions(self) -> list[EditRegion]:
        return [r for r in self.regions if r.w_pct >= 0.5 and r.h_pct >= 0.5]

    def done(self, r):
        for j in self._jobs:
            j.wait(3000)
        super().done(r)
