"""Hộp thoại “Chỉnh sửa vùng hình ảnh”: kéo chuột trên khung hình video để tạo/di chuyển/đổi cỡ
các vùng, chọn cách xử lý (nội suy nền / làm mờ / che), khoảng thời gian và xem trước.

- Toạ độ lưu theo % khung hình GỐC (đã loại phần viền đen khi ảnh được co vào khung widget).
- Xem trước: tối đa 1 tiến trình ffmpeg tại một thời điểm; chỉ giữ yêu cầu mới nhất; đóng hộp thoại thì
  huỷ yêu cầu chờ, dừng ffmpeg đang chạy, đợi luồng kết thúc rồi mới xoá thư mục tạm.
- Ba chế độ xem: khung gốc (để căn), chỉ xử lý vùng, thành phẩm (dùng cấu hình xuất hiện tại qua
  preview_frame — cùng filter_graph với Exporter, không có pipeline thứ hai)."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import theme
from .render import (
    Cover, EditRegion, Look, PreviewCancelled, SubStyle, _run_cancellable, preview_frame, probe,
)
from .transcribe import ffmpeg_exe

MODES = [("Nội suy nền (xoá)", "erase"), ("Làm mờ", "blur"), ("Che bằng màu", "box")]
VIEWS = [("Khung gốc (để căn vùng)", "raw"), ("Chỉ xử lý vùng", "regions"), ("Thành phẩm như video xuất", "final")]
GRAB = 7            # px: khoảng bắt cạnh/góc để đổi cỡ
MIN_PCT = 1.0       # vùng nhỏ hơn mức này (%) coi là bấm nhầm


def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


# ====================================================================== luồng xem trước
class _PreviewWorker(QThread):
    finished_frame = Signal(str, str, int)      # đường dẫn ảnh, lỗi, mã yêu cầu

    def __init__(self, fn, tag: int):
        super().__init__()
        self.fn, self.tag = fn, tag
        self.cancel = threading.Event()

    def run(self):
        try:
            self.finished_frame.emit(self.fn(self.cancel), "", self.tag)
        except PreviewCancelled:
            self.finished_frame.emit("", "cancelled", self.tag)
        except Exception as e:  # noqa: BLE001
            self.finished_frame.emit("", str(e)[:300], self.tag)


# ====================================================================== khung vẽ
class RegionCanvas(QWidget):
    changed = Signal()                  # vùng bị kéo / đổi cỡ / tạo mới
    selected = Signal(int)

    def __init__(self, regions: list[EditRegion]):
        super().__init__()
        self.regions = regions
        self.sel = -1
        self.pix: QPixmap | None = None
        self.frame_size: tuple[int, int] | None = None    # (w, h) khung GỐC, để quy đổi đúng tỉ lệ
        self.show_boxes = True
        self.editable = True
        self._drag = None               # (kiểu, chỉ số, điểm neo %, vùng ban đầu, cạnh)
        self.setMouseTracking(True)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # --- quy đổi toạ độ (bỏ viền đen do giữ tỉ lệ)
    def img_rect(self) -> QRectF:
        """Vùng ảnh thật trong widget; rỗng khi chưa có khung hình."""
        if not self.pix or self.pix.isNull():
            return QRectF()
        fw, fh = self.frame_size or (self.pix.width(), self.pix.height())
        k = min(self.width() / fw, self.height() / fh)
        w, h = fw * k, fh * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def to_pct(self, p: QPointF) -> tuple[float, float]:
        r = self.img_rect()
        x = (p.x() - r.x()) / r.width() * 100
        y = (p.y() - r.y()) / r.height() * 100
        return min(100.0, max(0.0, x)), min(100.0, max(0.0, y))

    def rect_of(self, g: EditRegion) -> QRectF:
        r = self.img_rect()
        return QRectF(r.x() + r.width() * g.x_pct / 100, r.y() + r.height() * g.y_pct / 100,
                      r.width() * g.w_pct / 100, r.height() * g.h_pct / 100)

    def _hit(self, pos: QPointF):
        """('move'|'resize', chỉ số, cạnh) — cạnh là các chữ l/r/t/b đang được kéo."""
        for i in range(len(self.regions) - 1, -1, -1):
            rr = self.rect_of(self.regions[i])
            if not rr.adjusted(-GRAB, -GRAB, GRAB, GRAB).contains(pos):
                continue
            edges = ""
            if abs(pos.x() - rr.left()) <= GRAB:
                edges += "l"
            elif abs(pos.x() - rr.right()) <= GRAB:
                edges += "r"
            if abs(pos.y() - rr.top()) <= GRAB:
                edges += "t"
            elif abs(pos.y() - rr.bottom()) <= GRAB:
                edges += "b"
            if edges:
                return "resize", i, edges
            if rr.contains(pos):
                return "move", i, ""
        return None, -1, ""

    @staticmethod
    def _cursor(kind, edges):
        if kind == "move":
            return Qt.SizeAllCursor
        if edges in ("lt", "rb"):
            return Qt.SizeFDiagCursor
        if edges in ("rt", "lb"):
            return Qt.SizeBDiagCursor
        return Qt.SizeHorCursor if edges in ("l", "r") else Qt.SizeVerCursor

    # --- chuột
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.editable or self.img_rect().isEmpty():
            return                       # đang xem kết quả hoặc chưa có khung hình: không sửa
        pos = e.position()
        kind, i, edges = self._hit(pos)
        if kind is None:
            if not self.img_rect().contains(pos):
                return                   # bấm vào viền đen: không tạo vùng
            x, y = self.to_pct(pos)
            self.regions.append(EditRegion(x, y, 0.0, 0.0))
            i, kind = len(self.regions) - 1, "create"
        self.sel = i
        self._drag = (kind, i, self.to_pct(pos), replace(self.regions[i]), edges)
        self.selected.emit(i)
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            if self.editable and not self.img_rect().isEmpty():
                kind, _, edges = self._hit(pos)
                self.setCursor(self._cursor(kind, edges) if kind else
                               (Qt.CrossCursor if self.img_rect().contains(pos) else Qt.ArrowCursor))
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        kind, i, (ax, ay), g0, edges = self._drag
        x, y = self.to_pct(pos)
        g = self.regions[i]
        if kind == "create":             # kéo theo cả 4 hướng từ điểm neo
            g.x_pct, g.y_pct = min(ax, x), min(ay, y)
            g.w_pct, g.h_pct = abs(x - ax), abs(y - ay)
        elif kind == "move":             # không cho ra ngoài khung
            g.x_pct = min(100 - g0.w_pct, max(0.0, g0.x_pct + x - ax))
            g.y_pct = min(100 - g0.h_pct, max(0.0, g0.y_pct + y - ay))
        else:                            # đổi cỡ từ cạnh/góc; kéo qua cạnh đối diện thì lật lại
            l, t, r, b = g0.x_pct, g0.y_pct, g0.x_pct + g0.w_pct, g0.y_pct + g0.h_pct
            if "l" in edges:
                l = x
            if "r" in edges:
                r = x
            if "t" in edges:
                t = y
            if "b" in edges:
                b = y
            g.x_pct, g.w_pct = min(l, r), abs(r - l)
            g.y_pct, g.h_pct = min(t, b), abs(b - t)
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        kind, i, _, g0, _ = self._drag
        g = self.regions[i]
        if g.w_pct < MIN_PCT or g.h_pct < MIN_PCT:
            if kind == "create":         # bấm nhầm: bỏ vùng vừa tạo
                del self.regions[i]
                self.sel = -1
                self.selected.emit(-1)
            else:                        # đổi cỡ quá nhỏ: trả lại cỡ cũ
                self.regions[i] = g0
        self._drag = None
        self.changed.emit()
        self.update()

    # --- vẽ
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#05070a"))
        r = self.img_rect()
        if r.isEmpty():
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Đang lấy khung hình…")
            return
        p.drawPixmap(r.toRect(), self.pix)
        if not self.show_boxes:
            return
        for i, g in enumerate(self.regions):
            rr = self.rect_of(g)
            col = QColor(theme.ACCENT if i == self.sel else theme.WARN)
            if not g.enabled:
                col = QColor(theme.MUTED)
            p.setPen(QPen(col, 2 if i == self.sel else 1.4, Qt.SolidLine if i == self.sel else Qt.DashLine))
            fill = QColor(col); fill.setAlpha(40)
            p.setBrush(fill)
            p.drawRect(rr)
            if i == self.sel:            # tay nắm ở 4 góc + 4 cạnh
                p.setBrush(col)
                for cx in (rr.left(), rr.center().x(), rr.right()):
                    for cy in (rr.top(), rr.center().y(), rr.bottom()):
                        if (cx, cy) != (rr.center().x(), rr.center().y()):
                            p.drawRect(QRectF(cx - 4, cy - 4, 8, 8))
            p.setPen(QColor("#ffffff"))
            p.drawText(rr.adjusted(4, 2, 0, 0), Qt.AlignLeft | Qt.AlignTop, str(i + 1))


# ====================================================================== hộp thoại
class RegionDialog(QDialog):
    def __init__(self, video: str, regions: list[EditRegion], parent=None, final_cfg=None):
        """final_cfg(): (cues, SubStyle, Cover, Look) theo cấu hình xuất hiện tại — cho chế độ “Thành phẩm”."""
        super().__init__(parent)
        self.setWindowTitle("Chỉnh sửa vùng hình ảnh")
        self.video = video
        self.final_cfg = final_cfg
        self.regions = [replace(r) for r in regions]          # bản sao: Huỷ không đụng tới cấu hình đã lưu
        self.info = probe(video)
        self.dur = float(self.info.get("duration") or 0)
        self._tmp = tempfile.mkdtemp(prefix="st_region_")
        self._tag = 0
        self._worker: _PreviewWorker | None = None
        self._pending: int | None = None     # yêu cầu mới nhất đang chờ (chỉ giữ 1)
        self._shown = ""                     # ảnh đang hiển thị
        self._closing = False
        self._loading = False
        self.stats = {"started": 0, "cancelled": 0}

        # --- khung hình + thời gian
        self.canvas = RegionCanvas(self.regions)
        self.canvas.frame_size = (self.info["w"], self.info["h"])
        self.slider = QSlider(Qt.Horizontal); self.slider.setRange(0, max(1, int(self.dur * 10)))
        self.lb_time = QLabel("0:00.0"); self.lb_time.setObjectName("chip"); self.lb_time.setMinimumWidth(64)
        self.lb_time.setAlignment(Qt.AlignCenter)
        self.cb_view = QComboBox()
        for label, v in VIEWS:
            self.cb_view.addItem(label, v)
        if not final_cfg:
            self.cb_view.model().item(2).setEnabled(False)
        self.lb_state = QLabel(""); self.lb_state.setObjectName("hint"); self.lb_state.setWordWrap(True)
        tl = QHBoxLayout(); tl.addWidget(self.slider, 1); tl.addWidget(self.lb_time)
        bl = QHBoxLayout(); lv = QLabel("Xem"); lv.setObjectName("fieldLabel")
        bl.addWidget(lv); bl.addWidget(self.cb_view); bl.addWidget(self.lb_state, 1)
        self.hint = QLabel(); self.hint.setObjectName("hint"); self.hint.setWordWrap(True)
        left = QVBoxLayout(); left.setSpacing(8)
        left.addWidget(self.canvas, 1); left.addLayout(tl); left.addLayout(bl); left.addWidget(self.hint)

        # --- danh sách + thuộc tính
        self.lst = QListWidget(); self.lst.setObjectName("partList"); self.lst.setMinimumHeight(80)
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst.setTextElideMode(Qt.ElideRight)
        self.btn_add = QPushButton("Thêm vùng"); self.btn_del = QPushButton("Xoá vùng")
        self.btn_add.setObjectName("ghost"); self.btn_del.setObjectName("ghost")
        ab = QHBoxLayout(); ab.addWidget(self.btn_add); ab.addWidget(self.btn_del)
        self.chk_on = QCheckBox("Bật vùng này")
        self.cb_mode = QComboBox()
        for label, m in MODES:
            self.cb_mode.addItem(label, m)
        self.sp_blur = QSpinBox(); self.sp_blur.setRange(2, 60)
        self.btn_color = QPushButton("Màu"); self.btn_color.setObjectName("ghost")
        self.sp_op = QSpinBox(); self.sp_op.setRange(10, 100); self.sp_op.setSuffix(" %"); self.sp_op.setFixedWidth(84)
        self.sp_start = QDoubleSpinBox(); self.sp_end = QDoubleSpinBox()
        for sp in (self.sp_start, self.sp_end):
            sp.setRange(0, max(0.1, round(self.dur, 1))); sp.setDecimals(1); sp.setSuffix(" s")
        self.sp_end.setSpecialValueText("hết video")
        self.btn_from_now = QPushButton("lúc này"); self.btn_to_now = QPushButton("lúc này")
        for b in (self.btn_from_now, self.btn_to_now):
            b.setObjectName("link"); b.setToolTip("Lấy thời điểm đang xem trên thanh thời gian")
        self.sp_xywh = []
        for _ in range(4):
            sp = QDoubleSpinBox(); sp.setRange(0, 100); sp.setDecimals(1); sp.setSuffix(" %")
            self.sp_xywh.append(sp)
        self.lb_err = QLabel(""); self.lb_err.setWordWrap(True)
        self.lb_err.setStyleSheet(f"color: {theme.DANGER};")

        def row(label, *ws):
            h = QHBoxLayout(); h.setSpacing(6)
            lb = QLabel(label); lb.setObjectName("fieldLabel"); lb.setFixedWidth(70)
            h.addWidget(lb)
            for x in ws:
                h.addWidget(x, 0 if isinstance(x, QPushButton) and x.objectName() == "link" else 1)
            return h
        self.props = QWidget(); pv = QVBoxLayout(self.props); pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(6)
        pv.addWidget(self.chk_on)
        pv.addLayout(row("Xử lý", self.cb_mode))
        self.row_blur = QWidget(); rb = QVBoxLayout(self.row_blur); rb.setContentsMargins(0, 0, 0, 0)
        rb.addLayout(row("Độ mờ", self.sp_blur))
        self.row_box = QWidget(); rx = QVBoxLayout(self.row_box); rx.setContentsMargins(0, 0, 0, 0)
        rx.addLayout(row("Màu che", self.btn_color, self.sp_op))
        pv.addWidget(self.row_blur); pv.addWidget(self.row_box)
        pv.addLayout(row("Từ", self.sp_start, self.btn_from_now))
        pv.addLayout(row("Đến", self.sp_end, self.btn_to_now))
        pv.addLayout(row("X / Y", self.sp_xywh[0], self.sp_xywh[1]))
        pv.addLayout(row("Rộng / Cao", self.sp_xywh[2], self.sp_xywh[3]))
        pv.addWidget(self.lb_err)
        right = QVBoxLayout(); right.setSpacing(8)
        t = QLabel("Các vùng"); t.setObjectName("cardTitle"); right.addWidget(t)
        right.addWidget(self.lst, 1); right.addLayout(ab); right.addWidget(self.props)
        note = QLabel("Chỉ dùng cho video của bạn hoặc video bạn được phép chỉnh sửa.")
        note.setObjectName("hint"); note.setWordWrap(True); right.addWidget(note)
        rw = QWidget(); rw.setLayout(right); rw.setFixedWidth(320)

        body = QHBoxLayout(); body.setSpacing(16); body.addLayout(left, 1); body.addWidget(rw)
        self.btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = self.btns.button(QDialogButtonBox.Ok); ok.setText("Lưu vùng"); ok.setObjectName("primary")
        self.btns.button(QDialogButtonBox.Cancel).setText("Huỷ")
        root = QVBoxLayout(self); root.setContentsMargins(16, 14, 16, 14); root.setSpacing(10)
        root.addLayout(body, 1); root.addWidget(self.btns)
        self.setMinimumSize(900, 580)
        self._fit_screen()

        # --- sự kiện
        self.btns.accepted.connect(self._try_accept); self.btns.rejected.connect(self.reject)
        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.timeout.connect(self._request)
        self.slider.valueChanged.connect(self._on_time)
        self.cb_view.currentIndexChanged.connect(self._on_view)
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
        self.slider.blockSignals(True)
        self.slider.setValue(min(self.slider.maximum(), 50))
        self.slider.blockSignals(False)
        self.lb_time.setText(_fmt(self._now()))
        self._on_view(0)

    def _fit_screen(self):
        scr = self.screen().availableGeometry() if self.screen() else None
        w, h = 1120, 680
        if scr is not None:
            w, h = min(w, scr.width() - 40), min(h, scr.height() - 60)
        self.resize(max(900, w), max(580, h))

    # ------------------------------------------------------------ xem trước (1 worker, giữ yêu cầu mới nhất)
    def _now(self) -> float:
        return self.slider.value() / 10

    def view(self) -> str:
        return self.cb_view.currentData()

    def _on_time(self, _):
        self.lb_time.setText(_fmt(self._now()))
        self._timer.start(150)

    def _on_view(self, _):
        v = self.view()
        self.canvas.editable = v == "raw"
        self.canvas.show_boxes = v == "raw"
        self.hint.setText("Kéo chuột trên khung hình để tạo vùng · kéo trong vùng để di chuyển · kéo cạnh/góc để "
                          "đổi cỡ · tua thanh thời gian để xem khung khác." if v == "raw" else
                          "Đang xem kết quả nên không sửa vùng trên khung hình. Chọn “Khung gốc” để căn vùng.")
        self.canvas.update()
        self._request()

    def _request(self):
        """Ghi nhận yêu cầu mới nhất; chưa có worker thì chạy ngay, có rồi thì dừng worker cũ và chờ nó xong."""
        if self._closing:
            return
        self._tag += 1
        self._pending = self._tag
        if self._worker and self._worker.isRunning():
            self._worker.cancel.set()
            return
        self._start_pending()

    def _job_fn(self, tag: int):
        t, v = self._now(), self.view()
        out = os.path.join(self._tmp, f"f{tag}.png")
        info, video = self.info, self.video
        regs = [replace(r) for r in self.regions if r.enabled and not r.time_error(self.dur)]
        if v == "raw":
            def fn(cancel):
                r = _run_cancellable([ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", video,
                                      "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", out], cancel=cancel)
                if r.returncode != 0 or not os.path.exists(out):
                    raise RuntimeError((r.stderr or "")[-200:] or "không lấy được khung hình")
                return out
        elif v == "regions":
            def fn(cancel):
                return preview_frame(video, [], SubStyle(), Cover(), t, out, info, max_w=1280,
                                     look=Look(regions=regs), cancel=cancel)
        else:
            cues, style, cover, look = self.final_cfg()
            look = replace(look, regions=regs)

            def fn(cancel):
                return preview_frame(video, cues, style, cover, t, out, info, max_w=1280, look=look, cancel=cancel)
        return fn

    def _start_pending(self):
        if self._pending is None or self._closing:
            return
        tag, self._pending = self._pending, None
        try:
            fn = self._job_fn(tag)
        except Exception as e:  # noqa: BLE001
            self.lb_state.setText(f"✖ {e}")
            return
        if self.view() != "raw":
            self.lb_state.setText("đang xử lý…")
        w = _PreviewWorker(fn, tag)
        w.finished_frame.connect(self._on_frame)
        w.finished.connect(self._on_worker_done)
        self._worker = w
        self.stats["started"] += 1
        w.start()

    def _on_worker_done(self):
        if not self._closing:
            self._start_pending()

    def _sweep(self, keep: str = ""):
        """Xoá mọi ảnh xem trước trừ ảnh đang hiển thị (gồm cả ảnh dở dang của ffmpeg bị dừng)."""
        for f in os.listdir(self._tmp) if os.path.isdir(self._tmp) else []:
            fp = os.path.join(self._tmp, f)
            if fp != keep:
                try:
                    os.remove(fp)
                except OSError:
                    pass

    def _on_frame(self, path, err, tag):
        if self._closing:
            return
        if err == "cancelled":
            self.stats["cancelled"] += 1
        if tag != self._tag or err:
            if err and err != "cancelled" and tag == self._tag:
                self.lb_state.setText(f"✖ {err}")
            self._sweep(keep=self._shown)
            return
        pm = QPixmap(path)
        self._shown = path
        self._sweep(keep=path)
        self.canvas.pix = pm
        self.lb_state.setText({"raw": "",
                               "regions": "Chỉ xử lý vùng (chưa gồm phụ đề, màu, khung, logo)",
                               "final": "Thành phẩm theo cấu hình xuất hiện tại, tại giây này của video gốc"}[self.view()])
        self.canvas.update()

    def running_workers(self) -> int:
        return int(bool(self._worker and self._worker.isRunning()))

    # ------------------------------------------------------------ danh sách
    def _label(self, i: int, g: EditRegion) -> str:
        mode = dict((m, l) for l, m in MODES)[g.mode].split(" (")[0]
        tt = "cả video" if not (g.start or g.end) else f"{_fmt(g.start)}–{_fmt(g.end) if g.end else 'hết'}"
        bad = " · ⚠ thời gian sai" if g.time_error(self.dur) else ""
        return f"{i + 1}. {mode} · {tt}" + ("" if g.enabled else " · tắt") + bad

    def _fill_list(self):
        self.lst.blockSignals(True)
        cur = self.canvas.sel
        self.lst.clear()
        for i, g in enumerate(self.regions):
            it = QListWidgetItem(self._label(i, g)); it.setToolTip(self._label(i, g))
            self.lst.addItem(it)
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

    def _after_edit(self):
        self._fill_list()
        if self.view() != "raw":
            self._timer.start(250)
        self.canvas.update()

    # ------------------------------------------------------------ thuộc tính
    def _load_props(self, i):
        ok = 0 <= i < len(self.regions)
        self.props.setEnabled(ok)
        self.btn_del.setEnabled(ok)
        if not ok:
            self.lb_err.setText("")
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
        self.lb_err.setText(g.time_error(self.dur))
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
        g.x_pct, g.y_pct = min(x, 100 - MIN_PCT), min(y, 100 - MIN_PCT)
        g.w_pct, g.h_pct = max(MIN_PCT, min(w, 100 - g.x_pct)), max(MIN_PCT, min(h, 100 - g.y_pct))
        self.row_blur.setVisible(g.mode == "blur"); self.row_box.setVisible(g.mode == "box")
        self.lb_err.setText(g.time_error(self.dur))
        self._after_edit()

    def _color_btn(self, c: str):
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

    # ------------------------------------------------------------ lưu / đóng
    def errors(self) -> list[str]:
        return [f"Vùng {i + 1}: {e}" for i, g in enumerate(self.regions) if (e := g.time_error(self.dur))]

    def _try_accept(self):
        errs = self.errors()
        if errs:
            from PySide6.QtWidgets import QMessageBox
            bad = next(i for i, g in enumerate(self.regions) if g.time_error(self.dur))
            self._select(bad)
            QMessageBox.warning(self, "Thời gian không hợp lệ", "\n".join(errs))
            return
        self.accept()

    def result_regions(self) -> list[EditRegion]:
        return [replace(r) for r in self.regions if r.w_pct >= MIN_PCT and r.h_pct >= MIN_PCT]

    def shutdown(self):
        """Dừng timer, bỏ yêu cầu chờ, dừng ffmpeg đang chạy, đợi luồng xong rồi mới xoá thư mục tạm."""
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._pending = None
        w = self._worker
        if w is not None:
            w.cancel.set()
            w.wait()                     # ffmpeg bị kill trong ≤0.1s nên luồng kết thúc ngay
        self.canvas.pix = None
        shutil.rmtree(self._tmp, ignore_errors=True)

    def done(self, r):
        self.shutdown()
        super().done(r)

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)
