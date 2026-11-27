"""Tab “Tìm & tải phim”: tìm video trên Bilibili như search.bilibili.com rồi tải về máy."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QSizePolicy, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QCheckBox, QComboBox, QDialog, QMenu, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from . import theme
from .sources import SITES, extract, search_url
from .bilibili import (
    BROWSE_CATS, BROWSE_MODES, DURATIONS, ORDERS, QUALITIES, Bili, BiliError, DownloadJob, Downloader, Part, Video, fmt_count,
    remove_temps,
)
from .bili_dialogs import LoginDialog, SubtitleDialog

QUICK = ["仙侠短剧", "修仙", "古装剧", "玄幻", "穿越", "短剧合集", "武侠"]
CARD_W = 214
THUMB_H = 120


def _card(title, step=None):
    from .gui import card
    return card(title, step)


def _field(label, widget, hint=None):
    from .gui import field
    return field(label, widget, hint)


def _btn(text, kind=None, tip=None):
    from .gui import btn
    return btn(text, kind, tip)


def _elide(label: QLabel, text: str, width: int):
    fm = label.fontMetrics()
    label.setText(fm.elidedText(text, Qt.ElideRight, width))
    label.setToolTip(text)


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


class _Bus(QObject):
    """Chuyển kết quả từ luồng nền về giao diện."""
    image = Signal(str, bytes)
    job = Signal(object)
    thumb = Signal(object, bytes)      # (mã dòng tải, ảnh bìa)


# ====================================================================== thẻ video
class VideoCard(QFrame):
    download = Signal(object)
    subtitles = Signal(object)

    def __init__(self, v: Video):
        super().__init__()
        self.v = v
        self.setObjectName("videoCard")
        self.setFixedWidth(CARD_W)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 10); lay.setSpacing(6)
        self.thumb = QLabel(); self.thumb.setObjectName("thumb")
        self.thumb.setFixedSize(CARD_W, THUMB_H); self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setText("…")
        self.badge = QLabel(v.duration, self.thumb); self.badge.setObjectName("badge")
        self.badge.adjustSize()
        self.badge.move(CARD_W - self.badge.width() - 8, THUMB_H - self.badge.height() - 8)
        lay.addWidget(self.thumb)
        body = QVBoxLayout(); body.setContentsMargins(12, 0, 12, 0); body.setSpacing(3)
        self.lb_vi = QLabel(); self.lb_vi.setObjectName("cardVi"); self.lb_vi.setWordWrap(True)
        self.lb_vi.setFixedHeight(38); self.lb_vi.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.lb_zh = QLabel(); self.lb_zh.setObjectName("muted")
        meta = f"{v.author} · ▶ {fmt_count(v.play)}"
        if v.pubdate:
            meta += " · " + time.strftime("%d/%m/%Y", time.localtime(v.pubdate))
        self.lb_meta = QLabel(); self.lb_meta.setObjectName("hint")
        body.addWidget(self.lb_vi); body.addWidget(self.lb_zh); body.addWidget(self.lb_meta)
        lay.addLayout(body)
        row = QHBoxLayout(); row.setContentsMargins(12, 2, 12, 0); row.setSpacing(6)
        self.btn_dl = _btn("Tải", "small", "Tải video này (video nhiều tập sẽ hỏi chọn tập)")
        self.btn_dl.setIcon(theme.icon("download", "#04150f", 14))
        self.btn_web = _btn("", "link", "Mở video trên trang Bilibili")
        self.btn_web.setIcon(theme.icon("link", theme.ACCENT, 16))
        self.btn_sub = _btn("Phụ đề", "link", "Lấy phụ đề có sẵn của video (SRT, VTT, TXT, JSON)")
        self.btn_sub.setIcon(theme.icon("caption", theme.ACCENT, 15))
        row.addWidget(self.btn_dl); row.addWidget(self.btn_sub); row.addStretch(1); row.addWidget(self.btn_web)
        lay.addLayout(row)
        self.btn_dl.clicked.connect(lambda: self.download.emit(self.v))
        self.btn_sub.clicked.connect(lambda: self.subtitles.emit(self.v))
        self.btn_web.clicked.connect(lambda: webbrowser.open(self.v.url))
        _elide(self.lb_meta, meta, CARD_W - 24)
        self.set_titles()

    def set_titles(self):
        main = self.v.title_vi or self.v.title
        fm = self.lb_vi.fontMetrics()
        w = CARD_W - 24  # tối đa ~2 dòng
        txt = main if fm.horizontalAdvance(main) <= w * 2 - 20 else fm.elidedText(main, Qt.ElideRight, w * 2 - 20)
        self.lb_vi.setText(txt)
        self.lb_vi.setToolTip(main)
        _elide(self.lb_zh, self.v.title if self.v.title_vi else "", CARD_W - 24)

    def set_image(self, data: bytes):
        pm = QPixmap()
        if pm.loadFromData(data):
            pm = pm.scaled(CARD_W, THUMB_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = max(0, (pm.width() - CARD_W) // 2); y = max(0, (pm.height() - THUMB_H) // 2)
            self.thumb.setPixmap(pm.copy(x, y, CARD_W, THUMB_H))
            self.badge.raise_()


# ====================================================================== hộp chọn tập
class PartsDialog(QDialog):
    def __init__(self, title: str, parts: list[Part], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn tập muốn tải")
        self.resize(560, 520)
        v = QVBoxLayout(self); v.setContentsMargins(18, 16, 18, 16); v.setSpacing(10)
        t = QLabel(title); t.setObjectName("cardTitle"); t.setWordWrap(True)
        n = QLabel(f"{len(parts)} tập/phần — bỏ tick những tập không muốn tải"); n.setObjectName("hint")
        self.lst = QListWidget(); self.lst.setObjectName("partList")
        for p in parts:
            dur = f"  ·  {p.duration // 60}:{p.duration % 60:02d}" if p.duration else ""
            it = QListWidgetItem(p.title + dur)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, p)
            self.lst.addItem(it)
        row = QHBoxLayout()
        b_all = _btn("Chọn tất cả", "link"); b_none = _btn("Bỏ chọn tất cả", "link")
        b_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        b_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        row.addWidget(b_all); row.addWidget(b_none); row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Thêm vào hàng chờ")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText("Hủy")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(t); v.addWidget(n); v.addLayout(row); v.addWidget(self.lst, 1); v.addWidget(bb)

    def _set_all(self, st):
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(st)

    def chosen(self) -> list[Part]:
        return [self.lst.item(i).data(Qt.UserRole) for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.Checked]


# ====================================================================== dòng trong hàng chờ
STATE_PILL = {"queued": ("Chờ", ""), "running": ("Đang tải", "run"), "paused": ("Tạm dừng", "warn"),
              "done": ("Xong", "done"), "error": ("Lỗi", "err"), "stopped": ("Đã dừng", "warn"), "removed": ("Đã bỏ", "")}


class JobRow(QFrame):
    """Một phim trong hàng chờ: ảnh bìa, tên, nhãn trạng thái màu, thanh tiến độ, nút Tạm dừng / Tiếp tục / Bỏ."""
    pause = Signal(object)
    resume = Signal(object)
    remove = Signal(object)
    TEXT_W = 252

    def __init__(self, job: DownloadJob):
        super().__init__()
        self.job = job
        self.setObjectName("jobRow")
        h = QHBoxLayout(self); h.setContentsMargins(8, 6, 8, 5); h.setSpacing(8)
        self.thumb = QLabel(); self.thumb.setObjectName("jobThumb"); self.thumb.setFixedSize(56, 32)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setPixmap(theme.glyph_pixmap("video", "#3a465c", 18))
        v = QVBoxLayout(); v.setSpacing(2)
        self.lb_title = QLabel(); self.lb_title.setObjectName("jobTitle")
        self.lb_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # tên dài không đẩy nút ra ngoài
        self.lb_title.setMinimumWidth(40)
        self.pill = QLabel(); self.pill.setObjectName("statusPill")
        trow = QHBoxLayout(); trow.setSpacing(6); trow.addWidget(self.lb_title, 1); trow.addWidget(self.pill)
        self.bar = QProgressBar(); self.bar.setFixedHeight(5)
        self.lb_info = QLabel(); self.lb_info.setObjectName("hint")
        self.lb_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lb_info.setContextMenuPolicy(Qt.NoContextMenu)
        self.btn_copy = _btn("Sao chép lỗi", "link", "Chép toàn bộ lỗi để gửi cho người hỗ trợ")
        self.btn_copy.setIcon(theme.icon("copy", theme.ACCENT, 14))
        self.btn_copy.clicked.connect(self._copy)
        self.btn_pause = _btn("Tạm dừng", "link", "Tạm dừng phim này; bấm Tiếp tục để tải tiếp từ chỗ dừng")
        self.btn_pause.setIcon(theme.icon("pause", theme.ACCENT, 14))
        self.btn_resume = _btn("Tiếp tục", "link")
        self.btn_resume.setIcon(theme.icon("play", theme.ACCENT, 14))
        self.btn_remove = _btn("Bỏ", "link", "Bỏ phim này khỏi hàng chờ (phim đang tải dở thì xoá phần đã tải)")
        self.btn_remove.setIcon(theme.icon("delete", "#8a94a7", 14))
        self.btn_remove.setStyleSheet("color:#8a94a7")
        self.btn_pause.clicked.connect(lambda: self.pause.emit(self.job))
        self.btn_resume.clicked.connect(lambda: self.resume.emit(self.job))
        self.btn_remove.clicked.connect(lambda: self.remove.emit(self.job))
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(0)
        self.trow, self.crow, self._compact = trow, crow, None
        for b in (self.btn_pause, self.btn_resume, self.btn_copy):
            crow.addWidget(b)
        crow.addStretch(1); crow.addWidget(self.btn_remove)
        v.addLayout(trow); v.addWidget(self.bar); v.addWidget(self.lb_info); v.addLayout(crow)
        h.addWidget(self.thumb, 0, Qt.AlignTop); h.addLayout(v, 1)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.refresh()

    def _place(self, compact: bool):
        """Gọn 1 dòng (chờ / tạm dừng / đã dừng / xong): nút nằm cạnh tên; đầy đủ (đang tải / lỗi): ảnh, thanh, chi tiết."""
        if compact == self._compact:
            return
        self._compact = compact
        btns = (self.btn_pause, self.btn_resume, self.btn_copy, self.btn_remove)
        if not hasattr(self, "_icons"):
            self._icons = {b: b.icon() for b in btns}
        for b in btns:                                   # dòng gọn: chỉ chữ, lề nhỏ -> vừa khung trên Windows
            b.setIcon(QIcon() if compact else self._icons[b])
            b.setStyleSheet(("padding: 2px 5px;" if compact else "") + ("color:#8a94a7" if b is self.btn_remove else ""))
        for b in btns:
            (self.trow if compact else self.crow).removeWidget(b)
        if compact:
            for b in btns:
                self.trow.addWidget(b)
        else:
            for b in btns[:3]:
                self.crow.insertWidget(self.crow.count() - 1, b)
            self.crow.addWidget(self.btn_remove)
        self.layout().setContentsMargins(*((8, 3, 6, 3) if compact else (8, 6, 8, 5)))
        self.thumb.setVisible(not compact)
        self.thumb.setFixedSize(*((56, 32) if not compact else (0, 0)))

    def set_thumb(self, data: bytes):
        pm = QPixmap()
        if pm.loadFromData(data):
            w, hh = self.thumb.width(), self.thumb.height()
            pm = pm.scaled(w, hh, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self.thumb.setPixmap(pm.copy(max(0, (pm.width() - w) // 2), max(0, (pm.height() - hh) // 2), w, hh))

    def _copy(self):
        j = self.job
        QGuiApplication.clipboard().setText(f"{j.title}\n{j.url}\n{j.info}")
        self.btn_copy.setText("Đã sao chép")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("Sao chép lỗi"))

    def _menu(self, pos):
        m = QMenu(self)
        j = self.job
        a_err = m.addAction("Sao chép lỗi" if j.state == "error" else "Sao chép trạng thái")
        a_url = m.addAction("Sao chép link video")
        a_dir = m.addAction("Mở thư mục chứa file") if j.file else None
        act = m.exec(self.mapToGlobal(pos))
        if act == a_err:
            self._copy()
        elif act == a_url:
            QGuiApplication.clipboard().setText(j.url)
        elif a_dir is not None and act == a_dir:
            folder = os.path.dirname(j.file)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(j.file)])
            else:
                subprocess.Popen(["xdg-open", folder])

    def refresh(self):
        j = self.job
        _elide(self.lb_title, j.title, self.TEXT_W - 84)
        self.lb_title.setToolTip(j.title)
        text, state = STATE_PILL.get(j.state, (j.state, ""))
        if j.state == "running" and j.cancel:
            text, state = "Đang dừng…", "warn"
        self.pill.setText(text); self.pill.setProperty("state", state)
        self.pill.style().unpolish(self.pill); self.pill.style().polish(self.pill)
        self.bar.setValue(int(j.pct))
        self.bar.setVisible(j.state in ("running", "paused", "stopped") or 0 < j.pct < 100)
        info = j.info or {"queued": "Đang chờ tới lượt", "stopped": "Đã dừng"}.get(j.state, "")
        if j.state == "running" and j.cancel:
            info = "Đang dừng…"
        self.btn_copy.setVisible(j.state == "error")
        self.btn_pause.setVisible(j.state in ("queued", "running") and not j.cancel)
        self.btn_resume.setVisible(j.state in ("paused", "stopped", "error"))
        # Tạm dừng -> Tiếp tục; Đã dừng -> Thử lại; Lỗi -> Tải lại (đều tải tiếp phần đã có)
        self.btn_resume.setText({"paused": "Tiếp tục", "stopped": "Thử lại"}.get(j.state, "Tải lại"))
        self.btn_resume.setToolTip("Tải tiếp phim này (giữ phần đã tải, không tải lại từ đầu)")
        self.btn_remove.setVisible(j.cancel != "remove")
        self.btn_remove.setText({"done": "Xoá khỏi danh sách", "stopped": "Xóa", "error": "Xóa"}.get(j.state, "Bỏ"))
        self.btn_remove.setToolTip("Xóa phim này khỏi hàng chờ (xoá phần tải dở, không đụng file đã xong)"
                                   if j.state != "done" else "Xoá khỏi danh sách (file đã tải vẫn giữ)")
        # Đang tải / lỗi: đầy đủ (ảnh, thanh, chi tiết). Còn lại: 1 dòng gọn, % đã tải ghi trên nhãn
        full = j.state in ("running", "error") or bool(j.cancel)
        self._place(not full)
        self.lb_info.setVisible(full)
        if not full:
            self.bar.setVisible(False)
            if j.state in ("paused", "stopped") and 0 < j.pct < 100:
                self.pill.setText(f"{text} {int(j.pct)}%")
            if j.state == "done":
                self.btn_remove.setText("Xoá")
            _elide(self.lb_title, j.title, 110 if j.state in ("paused", "stopped") else 140 if j.state == "queued" else 200)
        _elide(self.lb_info, info, self.TEXT_W)
        self.lb_info.setToolTip(j.info)
        color = {"error": "#ef5d6c"}.get(j.state, "")
        self.lb_info.setStyleSheet(f"color:{color}" if color else "")


class DownloadOptions(QDialog):
    """Tùy chọn tải (thư mục, chất lượng, tải kèm, cookie): gom vào một hộp thoại như 4K Video Downloader."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Tùy chọn tải")
        self.setMinimumWidth(460)
        self.lay = QVBoxLayout(self); self.lay.setSpacing(12)


# ====================================================================== luồng tải
class DownloadThread(QThread):
    """Tải lần lượt từng phim. Hàng chờ là danh sách (không phải queue) để tạm dừng / bỏ được phim đang chờ."""

    def __init__(self, bus: _Bus, log, bili: Bili):
        super().__init__()
        self.pending: list[DownloadJob] = []
        self.cv = threading.Condition()
        self.bus = bus
        self.dl = Downloader(on_update=lambda j: bus.job.emit(j), log=log, bili=bili)
        self.current: DownloadJob | None = None
        self._quit = False

    def add(self, job: DownloadJob):
        with self.cv:
            if job not in self.pending:
                self.pending.append(job)
            self.cv.notify()

    def _take(self, job: DownloadJob) -> bool:
        with self.cv:
            if job in self.pending:
                self.pending.remove(job)
                return True
        return False

    def pause(self, job: DownloadJob):
        if job is self.current:
            job.cancel = "pause"                      # luồng tải tự dừng và báo "paused"
            self.bus.job.emit(job)
        elif self._take(job):
            job.state, job.info = "paused", "Đã tạm dừng · chưa tải"
            self.bus.job.emit(job)

    def resume(self, job: DownloadJob):
        if job.state not in ("paused", "stopped", "error"):
            return
        job.cancel, job.state, job.info = "", "queued", ""
        self.bus.job.emit(job)
        self.add(job)

    def remove(self, job: DownloadJob):
        if job is self.current:
            job.cancel = "remove"                     # luồng tải dừng, xoá file tạm, báo "removed"
            self.bus.job.emit(job)
            return
        self._take(job)
        if job.state != "done":
            remove_temps(job)
        job.state, job.info = "removed", "Đã bỏ"
        self.bus.job.emit(job)

    def stop_all(self):
        self.dl.stop_event.set()
        with self.cv:
            jobs, self.pending = self.pending, []
        for j in jobs:
            j.state, j.info = "stopped", "Đã dừng"
            self.bus.job.emit(j)

    def run(self):
        while not self._quit:
            with self.cv:
                if not self.pending:
                    self.cv.wait(0.3)
                    continue
                job = self.pending.pop(0)
                if job.state != "queued":
                    continue
                self.current = job
            try:
                self.dl.stop_event.clear()
                self.dl.run(job)
            finally:
                self.current = None

    def shutdown(self):
        self._quit = True
        self.stop_all()
        self.wait(3000)


class QueueDialog(QDialog):
    """Bảng lớn: xem trọn mọi phim trong hàng chờ, cập nhật trực tiếp; chọn nhiều phim để tạm dừng / thử lại / xóa."""
    COLS = ["#", "Tên phim", "Trạng thái", "Tiến độ", "Chi tiết", "Thư mục lưu"]

    def __init__(self, page: "SearchPage"):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("Hàng chờ tải — tất cả phim")
        self.resize(1000, 560)
        v = QVBoxLayout(self); v.setContentsMargins(14, 14, 14, 14); v.setSpacing(10)
        self.lb = QLabel(); self.lb.setObjectName("stage")
        self.tb = QTableWidget(0, len(self.COLS)); self.tb.setHorizontalHeaderLabels(self.COLS)
        self.tb.verticalHeader().setVisible(False)
        self.tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch); hh.setSectionResizeMode(4, QHeaderView.Stretch)
        for c, w in ((0, 40), (2, 100), (3, 70), (5, 190)):
            self.tb.setColumnWidth(c, w)
        self.tb.cellDoubleClicked.connect(self._open)
        th = self.page.dthread
        b_p = _btn("Tạm dừng", "ghost"); b_r = _btn("Tiếp tục / Thử lại", "ghost"); b_x = _btn("Xóa khỏi hàng chờ", "ghost")
        b_p.clicked.connect(lambda: [th.pause(j) for j in self._selected() if j.state in ("queued", "running")])
        b_r.clicked.connect(lambda: [th.resume(j) for j in self._selected()])
        b_x.clicked.connect(lambda: [th.remove(j) for j in self._selected()])
        b_dir = _btn("Mở thư mục", "ghost"); b_close = _btn("Đóng", "primary")
        b_dir.clicked.connect(self.page._open_out); b_close.clicked.connect(self.close)
        br = QHBoxLayout(); br.addWidget(b_p); br.addWidget(b_r); br.addWidget(b_x); br.addStretch(1)
        br.addWidget(b_dir); br.addWidget(b_close)
        hint = QLabel("Chọn nhiều dòng bằng Ctrl / Shift. Bấm đúp một dòng để mở thư mục chứa phim."); hint.setObjectName("hint")
        v.addWidget(self.lb); v.addWidget(self.tb, 1); v.addWidget(hint); v.addLayout(br)

    def _jobs(self):
        return [r.job for r in self.page.rows.values()]

    def _selected(self):
        jobs = self._jobs()
        return [jobs[r] for r in sorted({i.row() for i in self.tb.selectedIndexes()}) if r < len(jobs)]

    def refresh(self):
        jobs = self._jobs()
        self.lb.setText(self.page.lb_qsum.text() or "Chưa có phim nào trong hàng chờ.")
        self.tb.setRowCount(len(jobs))
        for i, j in enumerate(jobs):
            info = j.info or ("Đang chờ tới lượt" if j.state == "queued" else "")
            if j.state == "done" and j.file:
                info = os.path.basename(j.file)
            vals = [str(i + 1), j.title, STATE_PILL.get(j.state, (j.state, ""))[0], f"{int(j.pct)}%", info, j.out_dir]
            for c, val in enumerate(vals):
                it = self.tb.item(i, c)
                if it is None:
                    it = QTableWidgetItem(); self.tb.setItem(i, c, it)
                if it.text() != val:
                    it.setText(val); it.setToolTip(val)

    def _open(self, row, _col):
        jobs = self._jobs()
        if row >= len(jobs):
            return
        j = jobs[row]
        if j.file and os.path.exists(j.file) and sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(j.file)])
        elif os.path.isdir(j.out_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(j.out_dir))


# ====================================================================== trang
class SearchPage(QWidget):
    def __init__(self, cfg: dict, save_cfg, log, providers_getter, bili: Bili | None = None):
        super().__init__()
        self.cfg, self.save_cfg, self.main_log = cfg, save_cfg, log
        self.providers_getter = providers_getter
        self.bili = bili or Bili()
        self.cards: list[VideoCard] = []
        self.page = 1
        self.num_pages = 1
        self.keyword = ""
        self.source: tuple = ()          # ("search", từ khóa) hoặc ("browse", kiểu, mã mục)
        self._auto_browsed = False
        self._browse_again: int | None = None
        self._old_jobs: list = []         # việc nền bị thay khi còn chạy: giữ tới khi xong (không huỷ giữa chừng)
        self._search_job: _Job | None = None
        self._tr_job: _Job | None = None
        self._img_pool = ThreadPoolExecutor(max_workers=6)
        self._gen = 0
        self.bus = _Bus()
        self.bus.image.connect(self._on_image)
        self.bus.job.connect(self._on_job)
        self.bus.thumb.connect(self._on_thumb)
        self.rows: dict[int, JobRow] = {}
        self.dthread = DownloadThread(self.bus, log, self.bili)
        self.dthread.start()
        self._build()
        st = self.cfg.get("search", {})
        self.ed_out.setText(st.get("out_dir") or str(Path.home() / "Videos" / "Bilibili"))
        self.cb_quality.setCurrentText(st.get("quality", "Cao nhất có thể"))
        self.chk_tr.setChecked(st.get("translate_titles", True))
        self.chk_cover.setChecked(st.get("cover", False))
        self.chk_subs.setChecked(st.get("subs", True))
        login = (self.cfg.get("bili_login") or {}).get("cookies") or ""
        if login:
            try:
                self.bili.set_login(json.loads(login))
            except (ValueError, TypeError):
                pass
        self._refresh_account(check=bool(login))
        self.cb_cookie.setCurrentText(st.get("cookie", "Không dùng"))
        if st.get("site") in SITES:
            self.cb_site.setCurrentText(st["site"])
        if st.get("browse_mode") in BROWSE_MODES:
            self.cb_bmode.setCurrentText(st["browse_mode"])
        if st.get("browse_cat") in BROWSE_CATS:
            self.cb_bcat.setCurrentText(st["browse_cat"])
        self._on_bmode()
        if self.cb_quality.currentIndex() < 0:
            self.cb_quality.setCurrentIndex(0)
        self._opt_summary()

    # ------------------------------------------------------------ giao diện
    def _build(self):
        # ---- thanh tìm kiếm
        top, tv = _card("Tìm phim", None)
        self.ed_q = QLineEdit(); self.ed_q.setObjectName("searchBox")
        self.ed_q.setPlaceholderText("Nhập từ khóa tiếng Trung (vd: 仙侠短剧) — hoặc gõ tiếng Việt rồi bấm VI→中")
        self.btn_vi = _btn("VI→中", "ghost", "Nhờ AI dịch từ khóa tiếng Việt sang tiếng Trung")
        self.cb_site = QComboBox(); self.cb_site.addItems(SITES); self.cb_site.setToolTip("Chọn trang để tìm phim")
        self.cb_order = QComboBox(); self.cb_order.addItems(ORDERS)
        self.cb_dur = QComboBox(); self.cb_dur.addItems(DURATIONS)
        self.btn_go = _btn("🔍  Tìm", "primary")
        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(self.cb_site); r1.addWidget(self.ed_q, 1); r1.addWidget(self.btn_vi); r1.addWidget(self.btn_go)
        r2 = QHBoxLayout(); r2.setSpacing(4)
        for k in QUICK:
            b = QPushButton(k); b.setObjectName("chipBtn"); b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"Gợi ý: tìm “{k}”")
            b.clicked.connect(lambda _=False, k=k: (self.ed_q.setText(k), self._search(1)))
            r2.addWidget(b)
        # độ rộng theo font của máy (Windows rộng hơn Linux) thay vì cố định px
        for cb in (self.cb_order, self.cb_dur):
            cb.ensurePolished()                          # áp stylesheet (cỡ chữ) trước khi đo
            fm = cb.fontMetrics()
            widest = max((fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())), default=80)
            cb.setFixedWidth(widest + 46)                # chữ dài nhất + lề + mũi tên, theo font thật của máy
        r2.addStretch(1); r2.addWidget(self.cb_order); r2.addWidget(self.cb_dur)
        self.chk_tr = QCheckBox("Dịch tiêu đề sang tiếng Việt")
        self.chk_tr.setToolTip("AI dịch tiêu đề kết quả (dùng nhà cung cấp AI ở Cài đặt AI)")
        self.ed_link = QLineEdit()
        self.ed_link.setPlaceholderText("Dán link hoặc cả đoạn chữ chia sẻ: Bilibili, Douyin, Tây Qua, Weibo, Tiểu Hồng Thư, AcFun…")
        self.btn_link = _btn("⬇  Tải link này", "ghost")
        self.btn_link_sub = _btn("📝 Lấy phụ đề", "ghost", "Lấy phụ đề có sẵn của link Bilibili (SRT, VTT, TXT, JSON)")
        r0 = QHBoxLayout(); r0.setSpacing(8); r0.addWidget(self.ed_link, 1); r0.addWidget(self.btn_link)
        r0.addWidget(self.btn_link_sub)
        self.cb_bmode = QComboBox(); self.cb_bmode.addItems(BROWSE_MODES)
        self.cb_bmode.setToolTip("Thịnh hành: video hot toàn Bilibili hôm nay\n"
                                 "Bảng xếp hạng: top 100 trong 3 ngày của mục đã chọn\n"
                                 "Đề xuất trong mục: mỗi lần bấm Trang sau là một loạt video mới")
        self.cb_bcat = QComboBox(); self.cb_bcat.addItems(BROWSE_CATS); self.cb_bcat.setMaxVisibleItems(20)
        self.cb_bcat.setToolTip("Mục (phân khu) của Bilibili")
        self.btn_browse = _btn("📂  Xem", "ghost", "Xem danh sách video theo mục, không cần gõ từ khóa")
        self.lb_browse = QLabel("Duyệt không cần gõ:"); self.lb_browse.setObjectName("hint")
        self.cb_bcat.setMinimumWidth(220); self.cb_bmode.setMinimumWidth(205)
        r3 = QHBoxLayout(); r3.setSpacing(8)
        for w in (self.lb_browse, self.cb_bmode, self.cb_bcat, self.btn_browse):
            r3.addWidget(w)
        r3.addStretch(1)
        tv.addLayout(r1); tv.addLayout(r2); tv.addLayout(r3); tv.addLayout(r0)

        # ---- kết quả
        res, rv = _card("Kết quả", None)
        self.lb_count = QLabel(""); self.lb_count.setObjectName("hint")
        self.lb_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lb_count.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)   # nhãn dài không bị cắt
        rv.itemAt(0).layout().addSpacing(10)
        rv.itemAt(0).layout().addWidget(self.chk_tr)
        rv.itemAt(0).layout().addWidget(self.lb_count, 1)
        self.grid_host = QWidget(); self.grid_host.setObjectName("gridHost")
        self.grid = QGridLayout(self.grid_host); self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14); self.grid.setVerticalSpacing(14)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.empty = QLabel("Nhập từ khóa rồi bấm Tìm, bấm một gợi ý, hoặc chọn mục ở hàng “Duyệt không cần gõ”.")
        self.empty.setObjectName("muted"); self.empty.setAlignment(Qt.AlignCenter)
        self.grid.addWidget(self.empty, 0, 0)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setWidget(self.grid_host)
        self.scroll.setFrameShape(QFrame.NoFrame); self.scroll.setObjectName("scroll")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rv.addWidget(self.scroll, 1)
        self.btn_prev = _btn("◀  Trang trước", "ghost"); self.btn_next = _btn("Trang sau  ▶", "ghost")
        self.lb_page = QLabel("Trang 1"); self.lb_page.setObjectName("chip")
        pr = QHBoxLayout(); pr.addStretch(1); pr.addWidget(self.btn_prev); pr.addWidget(self.lb_page)
        pr.addWidget(self.btn_next); pr.addStretch(1)
        rv.addLayout(pr)

        # ---- hàng chờ tải (tùy chọn gom vào một hộp thoại, danh sách là phần chính)
        side, sv = _card("Hàng chờ tải", None)
        side.setFixedWidth(400)
        self.btn_all = _btn("Xem tất cả", "link", "Mở bảng lớn xem toàn bộ phim: đang tải, chờ, tạm dừng, đã dừng, xong, lỗi")
        sv.itemAt(0).layout().addWidget(self.btn_all)
        self.opt_dlg = DownloadOptions(self)
        ol = self.opt_dlg.lay
        self.ed_out = QLineEdit(); self.btn_out = _btn("Chọn…", "ghost")
        orow = QHBoxLayout(); orow.setSpacing(8); orow.addWidget(self.ed_out, 1); orow.addWidget(self.btn_out)
        self.cb_quality = QComboBox(); self.cb_quality.addItems(QUALITIES)
        ol.addLayout(_field("Lưu vào thư mục", orow))
        ol.addLayout(_field("Chất lượng", self.cb_quality,
                            "Không cần đăng nhập vẫn tải được tới 1080p (1080P+, 4K, 60fps chỉ dành cho VIP)"))
        self.chk_cover = QCheckBox("Ảnh bìa (.jpg)")
        self.chk_cover.setToolTip("Tải kèm ảnh bìa của video")
        self.chk_subs = QCheckBox("Phụ đề có sẵn (.srt)")
        self.chk_subs.setToolTip("Lưu phụ đề Bilibili cạnh video thành “tên.zh.srt”: tab Dịch phụ đề dùng luôn, không phải nghe giọng.\n"
                                 "Cần đăng nhập Bilibili.")
        xrow = QHBoxLayout(); xrow.setSpacing(16)
        xrow.addWidget(self.chk_cover); xrow.addWidget(self.chk_subs); xrow.addStretch(1)
        ol.addLayout(_field("Tải kèm", xrow))
        self.cb_cookie = QComboBox(); self.cb_cookie.addItems(["Không dùng", "Firefox", "Edge", "Chrome"])
        ol.addLayout(_field("Cookie trình duyệt (cho Douyin…)", self.cb_cookie,
                            "Chỉ cần khi trang báo đòi cookie. Không dùng cho Bilibili."))
        b_ok = _btn("Xong", "primary"); b_ok.clicked.connect(self.opt_dlg.accept)
        okr = QHBoxLayout(); okr.addStretch(1); okr.addWidget(b_ok)
        ol.addLayout(okr)
        self.opt_dlg.finished.connect(lambda _=0: (self._store(), self._opt_summary()))

        self.btn_opts = QPushButton(); self.btn_opts.setObjectName("optSummary")
        self.btn_opts.setCursor(Qt.PointingHandCursor)
        self.btn_opts.setIcon(theme.icon("settings", theme.MUTED, 15))
        self.btn_opts.clicked.connect(self.opt_dlg.exec)
        sv.addWidget(self.btn_opts)
        self.lb_acc = QLabel(""); self.lb_acc.setObjectName("hint")
        self.btn_acc = _btn("🔑 Đăng nhập", "link")
        arow = QHBoxLayout(); arow.setSpacing(6); arow.addWidget(self.lb_acc, 1); arow.addWidget(self.btn_acc)
        sv.addLayout(arow)
        self.q_host = QWidget(); self.q_lay = QVBoxLayout(self.q_host)
        self.q_lay.setContentsMargins(0, 0, 0, 0); self.q_lay.setSpacing(6); self.q_lay.addStretch(1)
        self.q_empty = QLabel("Chưa có phim nào.\nBấm “⬇ Tải” ở một kết quả, hoặc dán link rồi bấm “Tải link này”.")
        self.q_empty.setObjectName("muted"); self.q_empty.setAlignment(Qt.AlignCenter)
        self.q_empty.setWordWrap(True); self.q_empty.setMinimumHeight(120)
        self.q_lay.insertWidget(0, self.q_empty)
        qs = QScrollArea(); qs.setWidgetResizable(True); qs.setWidget(self.q_host)
        qs.setFrameShape(QFrame.NoFrame); qs.setObjectName("scroll")
        qs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.q_scroll = qs
        sv.addWidget(qs, 1)
        self.lb_qsum = QLabel(""); self.lb_qsum.setObjectName("stage"); self.lb_qsum.setWordWrap(True)
        sv.addWidget(self.lb_qsum)
        b1 = _btn("", "link", "Mở thư mục lưu phim"); b1.setIcon(theme.icon("folder", theme.ACCENT, 17))
        b3 = _btn("Xoá mục xong", "link", "Xoá khỏi danh sách các phim đã xong / lỗi / đã dừng (không xoá file)")
        self.btn_pause_all = _btn("Dừng hết", "link", "Tạm dừng phim đang tải và các phim đang chờ (giữ phần đã tải)")
        self.btn_pause_all.setIcon(theme.icon("pause", theme.ACCENT, 15))
        self.btn_resume_all = _btn("Tiếp tục hết", "link", "Tải tiếp mọi phim đang tạm dừng / đã dừng")
        self.btn_resume_all.setIcon(theme.icon("play", theme.ACCENT, 15))
        brow = QHBoxLayout(); brow.setSpacing(0)
        brow.addWidget(b1); brow.addWidget(b3); brow.addStretch(1); brow.addWidget(self.btn_pause_all); brow.addWidget(self.btn_resume_all)
        sv.addLayout(brow)

        # Hàng chờ tải là cột riêng chạy hết chiều cao bên phải -> nhìn được nhiều phim cùng lúc
        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(top); left.addWidget(res, 1)
        root = QHBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addLayout(left, 1); root.addWidget(side)

        # ---- sự kiện
        self.btn_go.clicked.connect(lambda: self._search(1))
        self.ed_q.returnPressed.connect(lambda: self._search(1))
        self.btn_vi.clicked.connect(self._vi_to_zh)
        self.btn_link.clicked.connect(self._from_link)
        self.btn_link_sub.clicked.connect(self._subs_from_link)
        self.btn_acc.clicked.connect(self._account_clicked)
        self.chk_subs.toggled.connect(lambda _=False: self._store())
        self.cb_site.currentTextChanged.connect(self._on_site)
        self.ed_link.returnPressed.connect(self._from_link)
        self.btn_prev.clicked.connect(lambda: self._goto(self.page - 1))
        self.btn_next.clicked.connect(lambda: self._goto(self.page + 1))
        self.btn_browse.clicked.connect(lambda: self._browse(1))
        self.cb_bmode.currentTextChanged.connect(self._on_bmode)
        self.cb_bcat.currentIndexChanged.connect(lambda _=0: self.source[:1] == ("browse",) and self._browse(1))
        self.btn_out.clicked.connect(self._pick_out)
        b1.clicked.connect(self._open_out)
        self.btn_pause_all.clicked.connect(self._pause_all)
        self.btn_resume_all.clicked.connect(self._resume_all)
        b3.clicked.connect(self._clear_done)
        self.btn_all.clicked.connect(self._show_all)
        for w in (self.cb_order, self.cb_dur):
            w.currentIndexChanged.connect(lambda _=0: self.source[:1] == ("search",) and self._search(1))
        self._update_pager()

    # ------------------------------------------------------------ nguồn
    def _on_site(self, site: str):
        inapp = SITES[site]["search"] is None
        self.cb_order.setEnabled(inapp); self.cb_dur.setEnabled(inapp)
        for w in (self.lb_browse, self.cb_bmode, self.cb_bcat, self.btn_browse):
            w.setVisible(inapp)                     # duyệt theo mục chỉ có ở Bilibili
        self.btn_go.setText("🔍  Tìm" if inapp else "🌐  Mở trang tìm")
        if inapp:
            self.ed_q.setPlaceholderText("Nhập từ khóa tiếng Trung (vd: 仙侠短剧) — hoặc gõ tiếng Việt rồi bấm VI→中")
        else:
            self.ed_q.setPlaceholderText(f"Gõ từ khóa rồi bấm “Mở trang tìm”: chọn phim trên {site}, chép link, dán vào ô bên dưới")
            note = SITES[site].get("note", "")
            self.lb_count.setText(note)
        if not self.cards:
            self.empty.setText("Nhập từ khóa rồi bấm Tìm, bấm một gợi ý, hoặc chọn mục ở hàng “Duyệt không cần gõ”." if inapp else
                               f"Với {site}: gõ từ khóa → bấm “Mở trang tìm” → chọn phim trên trình duyệt → "
                               "chép link (hoặc cả đoạn chữ chia sẻ) → dán vào ô “Dán link…” ở trên.")
            self.empty.setWordWrap(True)
        self._store()

    def _open_site_search(self, kw: str):
        site = self.cb_site.currentText()
        url = search_url(site, kw)
        webbrowser.open(url)
        self.lb_count.setText(f"Đã mở trang tìm {site} trên trình duyệt. Chọn phim → chép link → dán vào ô “Dán link…” bên dưới.")
        self.ed_link.setFocus()

    # ------------------------------------------------------------ tìm kiếm
    def _search(self, page: int):
        kw = self.ed_q.text().strip()
        if not kw:
            self.ed_q.setFocus()
            return
        if SITES[self.cb_site.currentText()]["search"] is not None:
            self._open_site_search(kw)
            return
        if self._search_job and self._search_job.isRunning():
            return
        page = max(1, min(page, self.num_pages if self.source == ("search", kw) else 50))
        self.keyword = kw
        self.source = ("search", kw)
        order = ORDERS[self.cb_order.currentText()]
        dur = DURATIONS[self.cb_dur.currentText()]
        self.lb_count.setText("đang tìm…")
        self.btn_go.setEnabled(False)
        self._search_job = _Job(lambda: self.bili.search(kw, page, order, dur))
        self._search_job.result.connect(self._on_results)
        self._search_job.finished.connect(self._run_pending)
        self._search_job.start()

    def _keep(self, job, drop: bool = True):
        """Không để Python huỷ một QThread còn đang chạy (Qt sẽ tắt cả app: “QThread: Destroyed while thread
        is still running”). drop=True: bỏ kết quả về sau (dịch tiêu đề/từ khóa cũ); False: vẫn dùng (lấy tập, tải link)."""
        if job is not None and job.isRunning():
            if drop:
                try:
                    job.result.disconnect()
                except (RuntimeError, TypeError):
                    pass
            self._old_jobs.append(job)
            job.finished.connect(lambda j=job: self._old_jobs.remove(j) if j in self._old_jobs else None)

    def _run_pending(self):
        """Lựa chọn duyệt đổi trong lúc đang tải: chạy lại khi luồng tải THỰC SỰ xong."""
        if self._browse_again is not None and not (self._search_job and self._search_job.isRunning()):
            again, self._browse_again = self._browse_again, None
            self._browse(again)

    def _goto(self, page: int):
        if self.source[:1] == ("browse",):
            self._browse(page)
        elif self.source[:1] == ("search",):
            self._search(page)

    def _on_bmode(self, _text=""):
        mode = BROWSE_MODES[self.cb_bmode.currentText()]
        self.cb_bcat.setEnabled(mode != "popular")
        if self.source[:1] == ("browse",):
            self._browse(1)

    def _browse(self, page: int):
        """Duyệt Bilibili theo mục, không cần từ khóa."""
        if self._search_job and self._search_job.isRunning():
            self._browse_again = page          # đang tải: chạy lại theo lựa chọn mới nhất khi xong
            return
        mode = BROWSE_MODES[self.cb_bmode.currentText()]
        rid = BROWSE_CATS[self.cb_bcat.currentText()] if mode != "popular" else 0
        src = ("browse", mode, rid)
        page = max(1, min(page, self.num_pages if self.source == src else 1))
        self.source = src
        self.keyword = ""
        self.lb_count.setText("đang tải danh sách…")
        self.btn_go.setEnabled(False); self.btn_browse.setEnabled(False)
        self._search_job = _Job(lambda: self.bili.browse(mode, rid, page))
        self._search_job.result.connect(self._on_results)
        self._search_job.finished.connect(self._run_pending)
        self._search_job.start()
        self._store()

    def _source_label(self, res) -> str:
        if self.source[:1] == ("browse",):
            _, mode, rid = self.source
            name = {v: k for k, v in BROWSE_MODES.items()}[mode].split(" ", 1)[1]
            cat = "" if mode == "popular" else " · " + {v: k for k, v in BROWSE_CATS.items()}.get(rid, str(rid))
            extra = f" · {res.total} video" if mode == "rank" and not res.note else ""
            note = f" ({res.note})" if res.note else ""
            return f"{name}{cat}{extra}{note}"
        return f"“{self.keyword}” · khoảng {res.total:,} video".replace(",", ".")

    def showEvent(self, e):
        super().showEvent(e)
        # Mở tab lần đầu mà chưa có kết quả: tự hiện danh sách đang chọn (mặc định Thịnh hành) để chọn ngay
        if not self._auto_browsed and not self.cards and SITES[self.cb_site.currentText()]["search"] is None \
                and self.cfg.get("search", {}).get("auto_browse", True):
            self._auto_browsed = True
            QTimer.singleShot(300, lambda: self.source or self._browse(1))

    def _on_results(self, res, err):
        self.btn_go.setEnabled(True); self.btn_browse.setEnabled(True)
        if self._browse_again is not None:     # đã đổi lựa chọn trong lúc tải: bỏ kết quả cũ,
            return                             # _run_pending tải lại khi luồng xong hẳn
        if err:
            self.lb_count.setText("")
            self._show_empty(f"✖ {err}")
            self.main_log(f"✖ Tìm Bilibili lỗi: {err}")
            return
        self.page, self.num_pages = res.page, max(1, res.num_pages)
        self._gen += 1
        gen = self._gen
        for c in self.cards:
            c.setParent(None); c.deleteLater()
        self.cards = [VideoCard(v) for v in res.videos]
        for c in self.cards:
            c.download.connect(self._ask_download)
            c.subtitles.connect(self._ask_subtitles)
        self.lb_count.setText(self._source_label(res))
        if not self.cards:
            self._show_empty("Không tìm thấy video nào. Thử từ khóa khác." if self.source[:1] == ("search",)
                             else "Mục này hiện chưa có video. Thử mục khác.")
        else:
            self.empty.hide()
            self._relayout()
        self.scroll.verticalScrollBar().setValue(0)
        self._update_pager()
        for c in self.cards:
            self._img_pool.submit(self._load_image, gen, c.v.bvid, c.v.cover)
        if self.chk_tr.isChecked():
            self._translate_titles(gen)

    def _show_empty(self, text):
        for c in self.cards:
            c.setParent(None); c.deleteLater()
        self.cards = []
        self.empty.setText(text); self.empty.show()
        self.grid.addWidget(self.empty, 0, 0)

    def _load_image(self, gen, bvid, url):
        if gen != self._gen or not url:
            return
        try:
            data = self.bili.image(url)
        except Exception:  # noqa: BLE001
            return
        if gen == self._gen:
            self.bus.image.emit(bvid, data)

    def _on_image(self, bvid, data):
        for c in self.cards:
            if c.v.bvid == bvid:
                c.set_image(data)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._relayout)

    def _relayout(self):
        if not self.cards:
            return
        avail = self.scroll.viewport().width()
        cols = max(1, (avail + 14) // (CARD_W + 14))
        for i, c in enumerate(self.cards):
            self.grid.addWidget(c, i // cols, i % cols)

    def _update_pager(self):
        self.lb_page.setText(f"Trang {self.page} / {self.num_pages}")
        self.btn_prev.setEnabled(self.page > 1 and bool(self.source))
        self.btn_next.setEnabled(self.page < self.num_pages and bool(self.source))

    # ------------------------------------------------------------ dịch
    def _translate_titles(self, gen):
        try:
            provs = self.providers_getter()
        except Exception as e:  # noqa: BLE001
            self.lb_count.setText(self.lb_count.text() + f" · (chưa dịch tiêu đề: {e})")
            return
        from .srt import Cue
        from .translator import Settings, Translator
        cues = [Cue(i + 1, "", "", c.v.title) for i, c in enumerate(self.cards)]
        st = Settings(source_lang="Tiếng Trung", target_lang="Tiếng Việt", threads=2, batch_size=20,
                      custom_prompt="Đây là TIÊU ĐỀ video, không phải phụ đề. Dịch ngắn gọn, giữ tên phim/tên riêng "
                                    "theo âm Hán Việt, giữ số tập (第1集 = Tập 1).")

        def run():
            t = Translator(provs, st)
            t.cb.log = lambda m: None
            return t.translate_cues(cues)
        self.lb_count.setText(self.lb_count.text() + " · đang dịch tiêu đề…")
        self._keep(self._tr_job)
        self._tr_job = _Job(run)
        self._tr_job.result.connect(lambda res, err, gen=gen: self._on_titles(gen, res, err))
        self._tr_job.start()

    def _on_titles(self, gen, res, err):
        txt = self.lb_count.text().replace(" · đang dịch tiêu đề…", "")
        if gen != self._gen:
            return
        if err:
            self.lb_count.setText(txt + " · (dịch tiêu đề lỗi)")
            self.main_log(f"⚠ Dịch tiêu đề lỗi: {err}")
            return
        for i, c in enumerate(self.cards):
            vi = (res or {}).get(i + 1, "").strip()
            if vi:
                c.v.title_vi = vi
                c.set_titles()
        self.lb_count.setText(txt)

    def _vi_to_zh(self):
        text = self.ed_q.text().strip()
        if not text:
            return
        try:
            prov = self.providers_getter()[0]
        except Exception as e:  # noqa: BLE001
            self.lb_count.setText(f"✖ {e}")
            return
        self.btn_vi.setEnabled(False)
        self.lb_count.setText("đang dịch từ khóa…")
        sys_p = ("Bạn chuyển từ khóa tìm phim tiếng Việt thành từ khóa tìm kiếm tiếng Trung giản thể "
                 "mà người Trung hay dùng trên Bilibili. Chỉ trả về đúng từ khóa, không giải thích, không ngoặc kép.")
        self._keep(self._kw_job)
        self._kw_job = _Job(lambda: prov.chat(sys_p, text))
        self._kw_job.result.connect(self._on_kw)
        self._kw_job.start()

    def _on_kw(self, res, err):
        self.btn_vi.setEnabled(True)
        if err:
            self.lb_count.setText(f"✖ Không dịch được: {err[:120]}")
            return
        kw = str(res).strip().strip('"“”「」').splitlines()[0][:60]
        self.lb_count.setText(f"Từ khóa: {kw}")
        self.ed_q.setText(kw)
        self._search(1)

    # ------------------------------------------------------------ tải
    def _from_link(self):
        text = self.ed_link.text().strip()
        if not text:
            return
        try:
            url, site = extract(text)
        except ValueError as e:
            self.lb_count.setText(f"✖ {e}")
            return
        if site != "Bilibili":
            out = os.path.join(self.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili"),
                               site.split(" (")[0])
            cb = self.cb_cookie.currentText().lower()
            job = DownloadJob(url, f"{site}: {url[:60]}", out, self.cb_quality.currentText(),
                              auto_title=True, cookies_browser="" if cb.startswith("không") else cb)
            self._enqueue(job)
            self.ed_link.clear()
            self.lb_count.setText(f"Đã thêm vào hàng chờ ({site}). {SITES[site].get('note', '')}")
            self._store()
            return
        text = url
        self.btn_link.setEnabled(False)
        self.lb_count.setText("đang đọc link…")

        def work(text=text):
            bvid, p = self.bili.resolve(text)
            meta = self.bili.video_meta(bvid)
            return bvid, p, meta
        self._keep(self._link_job, drop=False)
        self._link_job = _Job(work)
        self._link_job.result.connect(self._on_link)
        self._link_job.start()

    def _on_link(self, res, err):
        self.btn_link.setEnabled(True)
        if err:
            self.lb_count.setText(f"✖ {err}")
            return
        bvid, p, meta = res
        pic = meta.get("pic") or ""
        v = Video(bvid=bvid, title=meta.get("title") or bvid, author=(meta.get("owner") or {}).get("name", ""),
                  cover=pic, duration="", play=int((meta.get("stat") or {}).get("view") or 0),
                  pubdate=int(meta.get("pubdate") or 0))
        self.ed_link.clear()
        if p:   # link đã chỉ đúng một phần (?p=N) -> tải phần đó
            pages = meta.get("pages") or []
            part = next((x for x in pages if x.get("page") == p), None)
            name = f"{v.title} - P{p} {part.get('part', '')}".strip() if part and len(pages) > 1 else v.title
            out = self.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili")
            self._enqueue(DownloadJob(f"{self.bili.web}/video/{bvid}?p={p}", name, out,
                                      self.cb_quality.currentText(), cover=self.chk_cover.isChecked(),
                                      subs=self.chk_subs.isChecked(), thumb_url=pic))
            self.lb_count.setText(f"Đã thêm vào hàng chờ: {name}")
            return
        self._ask_download(v)

    def _ask_download(self, v: Video):
        self.lb_count.setText("đang lấy danh sách tập…")
        self._keep(self._parts_job, drop=False)
        self._parts_job = _Job(lambda: self.bili.parts(v.bvid))
        self._parts_job.result.connect(lambda res, err, v=v: self._on_parts(v, res, err))
        self._parts_job.start()

    def _on_parts(self, v: Video, res, err):
        self.lb_count.setText("")
        if err:
            # không lấy được danh sách -> tải luôn link video
            self.main_log(f"⚠ {err} — tải trực tiếp {v.url}")
            title, parts = v.title, [Part(v.url, v.title)]
        else:
            title, parts = res
        if len(parts) > 1:
            dlg = PartsDialog(v.title_vi or title, parts, self)
            if dlg.exec() != QDialog.Accepted:
                return
            parts = dlg.chosen()
        out = self.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili")
        if len(parts) > 1:
            safe = "".join(ch for ch in (v.title_vi or title) if ch not in '\\/:*?"<>|').strip()[:80]
            out = os.path.join(out, safe or v.bvid)
        for p in parts:
            name = p.title if len(parts) > 1 else (v.title_vi or v.title)
            self._enqueue(DownloadJob(p.url, name, out, self.cb_quality.currentText(), cover=self.chk_cover.isChecked(),
                                      subs=self.chk_subs.isChecked(), thumb_url=v.cover))
        self._store()

    # ------------------------------------------------------------ phụ đề & đăng nhập
    def _subs_from_link(self):
        text = self.ed_link.text().strip()
        if not text:
            self.lb_count.setText("Dán link Bilibili vào ô bên trái rồi bấm “Lấy phụ đề”.")
            return
        try:
            url, site = extract(text)
        except ValueError as e:
            self.lb_count.setText(f"✖ {e}")
            return
        if site != "Bilibili":
            self.lb_count.setText("✖ Chỉ lấy được phụ đề của Bilibili.")
            return
        self.btn_link_sub.setEnabled(False)
        self.lb_count.setText("đang đọc link…")

        def work(url=url):
            bvid, p = self.bili.resolve(url)
            title, parts = self.bili.parts(bvid)
            return bvid, p, title, parts
        self._keep(getattr(self, '_sublink_job', None), drop=False)
        self._sublink_job = _Job(work)
        self._sublink_job.result.connect(self._on_sub_link)
        self._sublink_job.start()

    def _on_sub_link(self, res, err):
        self.btn_link_sub.setEnabled(True)
        if err:
            self.lb_count.setText(f"✖ {err}")
            return
        self.lb_count.setText("")
        bvid, p, title, parts = res
        if p:   # link chỉ đúng phần ?p=N -> mở sẵn phần đó
            parts = sorted(parts, key=lambda x: x.index != p)
        self._open_subtitles(title, parts)

    def _ask_subtitles(self, v: Video):
        self.lb_count.setText("đang lấy danh sách tập…")
        self._keep(getattr(self, '_subparts_job', None), drop=False)
        self._subparts_job = _Job(lambda: self.bili.parts(v.bvid))
        self._subparts_job.result.connect(lambda res, err, v=v: self._on_sub_parts(v, res, err))
        self._subparts_job.start()

    def _on_sub_parts(self, v: Video, res, err):
        self.lb_count.setText("")
        title, parts = (v.title, [Part(v.url, v.title)]) if err else res
        self._open_subtitles(v.title_vi or title, parts)

    def _open_subtitles(self, title: str, parts: list):
        out = self.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili")
        if len(parts) > 1:
            safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|').strip()[:80]
            out = os.path.join(out, safe or "phu_de")
        dlg = SubtitleDialog(self.bili, title, parts, out, self)
        dlg.login_changed.connect(self._on_login)
        dlg.exec()

    def _account_clicked(self):
        if self.bili.logged_in():
            self.bili.logout()
            self.cfg.pop("bili_login", None)
            self.save_cfg()
            self._refresh_account()
            self.main_log("Đã đăng xuất Bilibili")
            return
        if LoginDialog(self.bili, self).exec() == QDialog.Accepted:
            self._on_login()

    def _on_login(self):
        self.cfg["bili_login"] = {"cookies": json.dumps(self.bili.login_cookies())}
        self.save_cfg()
        self._refresh_account(check=True)

    def _refresh_account(self, check: bool = False):
        if not self.bili.logged_in():
            self.lb_acc.setText("Bilibili: chưa đăng nhập")
            self.lb_acc.setToolTip("Chưa đăng nhập: tải được tới 1080p nhưng không lấy được phụ đề.\n"
                                   "Đăng nhập (quét mã QR bằng app Bilibili) để lấy phụ đề; tài khoản VIP tải được 1080P+, 4K.")
            self.btn_acc.setText("🔑 Đăng nhập")
            return
        self.btn_acc.setText("Đăng xuất")
        if not check:
            return
        self.lb_acc.setText("Bilibili: đang kiểm tra đăng nhập…")
        self._keep(getattr(self, '_acc_job', None), drop=False)
        self._acc_job = _Job(self.bili.account)
        self._acc_job.result.connect(self._on_account)
        self._acc_job.start()

    def _on_account(self, acc, err):
        if err:
            self.lb_acc.setText("Bilibili: đã lưu đăng nhập (chưa kiểm tra được mạng)")
            return
        if not acc["login"]:
            self.bili.logout()
            self.cfg.pop("bili_login", None)
            self.save_cfg()
            self.lb_acc.setText("Bilibili: đăng nhập đã hết hạn, bấm Đăng nhập để quét mã lại")
            self.btn_acc.setText("🔑 Đăng nhập")
            return
        self.lb_acc.setText(f"Bilibili: ✔ {acc['name']}" + (" · VIP" if acc["vip"] else ""))

    def _enqueue(self, job: DownloadJob):
        row = JobRow(job)
        row.pause.connect(self.dthread.pause)
        row.resume.connect(self.dthread.resume)
        row.remove.connect(self.dthread.remove)
        if job.thumb_url:
            self._img_pool.submit(self._load_thumb, id(job), job.thumb_url)
        self.rows[id(job)] = row
        self.q_empty.hide()
        self.q_lay.insertWidget(self.q_lay.count() - 1, row)
        self.dthread.add(job)
        self._summary()

    def _on_job(self, job: DownloadJob):
        row = self.rows.get(id(job))
        if job.state == "removed":
            if row:
                row.setParent(None); row.deleteLater(); del self.rows[id(job)]
            if not self.rows:
                self.q_empty.show()
            self._summary()
            return
        if row:
            row.refresh()
            if job.state == "running":                       # tự cuộn tới phim đang tải
                QTimer.singleShot(0, lambda r=row: self.q_scroll.ensureWidgetVisible(r, 0, 40))
        if job.state == "done":
            self.main_log(f"✔ Đã tải: {job.file}")
        elif job.state == "error":
            self.main_log(f"✖ Tải lỗi ({job.title}): {job.info}")
        self._summary()

    def _load_thumb(self, key, url):
        try:
            self.bus.thumb.emit(key, self.bili.image(url))
        except Exception:  # noqa: BLE001
            pass

    def _on_thumb(self, key, data: bytes):
        row = self.rows.get(key)
        if row:
            row.set_thumb(data)

    def _pause_all(self):
        for r in list(self.rows.values()):
            if r.job.state in ("queued", "running"):
                self.dthread.pause(r.job)

    def _resume_all(self):
        for r in list(self.rows.values()):
            if r.job.state in ("paused", "stopped"):
                self.dthread.resume(r.job)

    def _opt_summary(self):
        """Dòng tóm tắt tùy chọn tải (bấm vào để đổi)."""
        extras = [x for x, on in (("phụ đề", self.chk_subs.isChecked()), ("ảnh bìa", self.chk_cover.isChecked())) if on]
        folder = Path(self.ed_out.text().strip() or "~")
        short = str(folder) if len(folder.parts) <= 2 else "…\\" + "\\".join(folder.parts[-2:])
        parts = [self.cb_quality.currentText()]
        if extras:
            parts.append("kèm " + ", ".join(extras))
        if not self.cb_cookie.currentText().startswith("Không"):
            parts.append("cookie " + self.cb_cookie.currentText())
        parts.append(short)
        self.btn_opts.setText("  " + " · ".join(parts))
        self.btn_opts.setToolTip("Bấm để đổi: thư mục lưu, chất lượng, tải kèm phụ đề / ảnh bìa, cookie\n"
                                 + str(folder))

    def _show_all(self):
        if getattr(self, "_all_dlg", None) is None:
            self._all_dlg = QueueDialog(self)
        self._all_dlg.refresh()
        self._all_dlg.show(); self._all_dlg.raise_(); self._all_dlg.activateWindow()

    def _summary(self):
        n = len(self.rows)
        self.btn_all.setText(f"Xem tất cả ({n})" if n else "Xem tất cả")
        self._summary_text()
        if getattr(self, "_all_dlg", None) is not None and self._all_dlg.isVisible():
            self._all_dlg.refresh()

    def _summary_text(self):
        jobs = [r.job for r in self.rows.values()]
        if not jobs:
            self.lb_qsum.setText("")
            return
        c = {s: sum(1 for j in jobs if j.state == s) for s in ("queued", "running", "paused", "done", "error", "stopped")}
        parts = [f"{c['done']}/{len(jobs)} xong"]
        if c["running"]:
            parts.append("đang tải 1")
        if c["queued"]:
            parts.append(f"{c['queued']} đang chờ")
        if c["paused"]:
            parts.append(f"{c['paused']} tạm dừng")
        if c["stopped"]:
            parts.append(f"{c['stopped']} đã dừng")
        if c["error"]:
            parts.append(f"{c['error']} lỗi")
        self.lb_qsum.setText(" · ".join(parts))

    def _clear_done(self):
        for k, r in list(self.rows.items()):
            if r.job.state in ("done", "error", "stopped", "removed"):
                r.setParent(None); r.deleteLater(); del self.rows[k]
        if not self.rows:
            self.q_empty.show()
        self._summary()

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu phim", self.ed_out.text())
        if d:
            self.ed_out.setText(d)
            self._store()

    def _open_out(self):
        folder = self.ed_out.text().strip()
        if not folder:
            return
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    # ------------------------------------------------------------ lưu / tắt
    def _store(self):
        self.cfg["search"] = {"out_dir": self.ed_out.text().strip(), "quality": self.cb_quality.currentText(),
                              "translate_titles": self.chk_tr.isChecked(), "cover": self.chk_cover.isChecked(),
                              "subs": self.chk_subs.isChecked(),
                              "site": self.cb_site.currentText(), "cookie": self.cb_cookie.currentText(),
                              "browse_mode": self.cb_bmode.currentText(), "browse_cat": self.cb_bcat.currentText(),
                              "auto_browse": self.cfg.get("search", {}).get("auto_browse", True)}

    def store_state(self):
        self._store()

    def busy(self) -> bool:
        return any(r.job.state in ("running", "queued") for r in self.rows.values())

    def shutdown(self):
        self._store()
        self.dthread.shutdown()
        self._img_pool.shutdown(wait=False, cancel_futures=True)
