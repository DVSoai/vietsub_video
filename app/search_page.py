"""Tab “Tìm & tải phim”: tìm video trên Bilibili như search.bilibili.com rồi tải về máy."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QMenu, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .bilibili import (
    DURATIONS, ORDERS, QUALITIES, Bili, BiliError, DownloadJob, Downloader, Part, Video, fmt_count,
)

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


# ====================================================================== thẻ video
class VideoCard(QFrame):
    download = Signal(object)

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
        self.btn_dl = _btn("⬇  Tải", "small"); self.btn_web = _btn("Mở web", "link")
        row.addWidget(self.btn_dl); row.addStretch(1); row.addWidget(self.btn_web)
        lay.addLayout(row)
        self.btn_dl.clicked.connect(lambda: self.download.emit(self.v))
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
class JobRow(QFrame):
    def __init__(self, job: DownloadJob):
        super().__init__()
        self.job = job
        self.setObjectName("jobRow")
        v = QVBoxLayout(self); v.setContentsMargins(12, 9, 12, 9); v.setSpacing(5)
        self.lb_title = QLabel(); self.lb_title.setObjectName("jobTitle")
        self.bar = QProgressBar(); self.bar.setFixedHeight(6)
        self.lb_info = QLabel(); self.lb_info.setObjectName("hint")
        self.lb_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lb_info.setContextMenuPolicy(Qt.NoContextMenu)
        self.btn_copy = _btn("📋 Sao chép lỗi", "link", "Chép toàn bộ lỗi để gửi cho người hỗ trợ")
        self.btn_copy.clicked.connect(self._copy)
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0); crow.addStretch(1); crow.addWidget(self.btn_copy)
        v.addWidget(self.lb_title); v.addWidget(self.bar); v.addWidget(self.lb_info); v.addLayout(crow)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.refresh()

    def _copy(self):
        j = self.job
        QGuiApplication.clipboard().setText(f"{j.title}\n{j.url}\n{j.info}")
        self.btn_copy.setText("✔ Đã sao chép")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("📋 Sao chép lỗi"))

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
        icon = {"queued": "⏳", "running": "⬇", "done": "✔", "error": "✖", "stopped": "■"}[j.state]
        _elide(self.lb_title, f"{icon}  {j.title}", 300)
        self.bar.setValue(int(j.pct))
        info = j.info or {"queued": "Đang chờ", "stopped": "Đã dừng"}.get(j.state, "")
        self.btn_copy.setVisible(j.state == "error")
        _elide(self.lb_info, info, 300)
        self.lb_info.setToolTip(j.info)
        color = {"done": "#21c49a", "error": "#ef5d6c", "stopped": "#f2b84b"}.get(j.state, "")
        self.lb_info.setStyleSheet(f"color:{color}" if color else "")


# ====================================================================== luồng tải
class DownloadThread(QThread):
    def __init__(self, bus: _Bus, log, bili: Bili):
        super().__init__()
        self.q: "queue.Queue[DownloadJob]" = queue.Queue()
        self.bus = bus
        self.dl = Downloader(on_update=lambda j: bus.job.emit(j), log=log, bili=bili)
        self._quit = False

    def add(self, job: DownloadJob):
        self.q.put(job)

    def stop_all(self):
        self.dl.stop_event.set()
        while not self.q.empty():
            try:
                j = self.q.get_nowait()
                j.state, j.info = "stopped", "Đã dừng"
                self.bus.job.emit(j)
            except queue.Empty:
                break

    def run(self):
        while not self._quit:
            try:
                job = self.q.get(timeout=0.3)
            except queue.Empty:
                continue
            if job.state == "stopped":
                continue
            self.dl.stop_event.clear()
            self.dl.run(job)

    def shutdown(self):
        self._quit = True
        self.stop_all()
        self.wait(3000)


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
        self._search_job: _Job | None = None
        self._tr_job: _Job | None = None
        self._img_pool = ThreadPoolExecutor(max_workers=6)
        self._gen = 0
        self.bus = _Bus()
        self.bus.image.connect(self._on_image)
        self.bus.job.connect(self._on_job)
        self.rows: dict[int, JobRow] = {}
        self.dthread = DownloadThread(self.bus, log, self.bili)
        self.dthread.start()
        self._build()
        st = self.cfg.get("search", {})
        self.ed_out.setText(st.get("out_dir") or str(Path.home() / "Videos" / "Bilibili"))
        self.cb_quality.setCurrentText(st.get("quality", "Cao nhất có thể"))
        self.chk_tr.setChecked(st.get("translate_titles", True))
        self.chk_cover.setChecked(st.get("cover", False))
        if self.cb_quality.currentIndex() < 0:
            self.cb_quality.setCurrentIndex(0)

    # ------------------------------------------------------------ giao diện
    def _build(self):
        # ---- thanh tìm kiếm
        top, tv = _card("Tìm phim trên Bilibili", None)
        self.ed_q = QLineEdit(); self.ed_q.setObjectName("searchBox")
        self.ed_q.setPlaceholderText("Nhập từ khóa tiếng Trung (vd: 仙侠短剧) — hoặc gõ tiếng Việt rồi bấm VI→中")
        self.btn_vi = _btn("VI→中", "ghost", "Nhờ AI dịch từ khóa tiếng Việt sang tiếng Trung")
        self.cb_order = QComboBox(); self.cb_order.addItems(ORDERS)
        self.cb_dur = QComboBox(); self.cb_dur.addItems(DURATIONS)
        self.btn_go = _btn("🔍  Tìm", "primary")
        r1 = QHBoxLayout(); r1.setSpacing(8)
        r1.addWidget(self.ed_q, 1); r1.addWidget(self.btn_vi); r1.addWidget(self.cb_order)
        r1.addWidget(self.cb_dur); r1.addWidget(self.btn_go)
        r2 = QHBoxLayout(); r2.setSpacing(6)
        lab = QLabel("Gợi ý:"); lab.setObjectName("hint"); r2.addWidget(lab)
        for k in QUICK:
            b = QPushButton(k); b.setObjectName("chipBtn"); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=k: (self.ed_q.setText(k), self._search(1)))
            r2.addWidget(b)
        r2.addStretch(1)
        self.chk_tr = QCheckBox("Dịch tiêu đề sang tiếng Việt (dùng nhà cung cấp AI ở tab Dịch phụ đề)")
        r2.addWidget(self.chk_tr)
        self.ed_link = QLineEdit()
        self.ed_link.setPlaceholderText("Hoặc dán link để tải luôn: bilibili.com/video/BV…, b23.tv/…, BV1xx…, av123…")
        self.btn_link = _btn("⬇  Tải link này", "ghost")
        r0 = QHBoxLayout(); r0.setSpacing(8); r0.addWidget(self.ed_link, 1); r0.addWidget(self.btn_link)
        tv.addLayout(r1); tv.addLayout(r2); tv.addLayout(r0)

        # ---- kết quả
        res, rv = _card("Kết quả", None)
        self.lb_count = QLabel(""); self.lb_count.setObjectName("hint")
        rv.itemAt(0).layout().addWidget(self.lb_count)
        self.grid_host = QWidget(); self.grid_host.setObjectName("gridHost")
        self.grid = QGridLayout(self.grid_host); self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14); self.grid.setVerticalSpacing(14)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.empty = QLabel("Nhập từ khóa rồi bấm Tìm, hoặc bấm một gợi ý ở trên.")
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

        # ---- hàng chờ tải
        side, sv = _card("Hàng chờ tải", None)
        side.setFixedWidth(360)
        self.ed_out = QLineEdit(); self.btn_out = _btn("Chọn…", "ghost")
        orow = QHBoxLayout(); orow.setSpacing(8); orow.addWidget(self.ed_out, 1); orow.addWidget(self.btn_out)
        self.cb_quality = QComboBox(); self.cb_quality.addItems(QUALITIES)
        sv.addLayout(_field("Lưu vào thư mục", orow))
        sv.addLayout(_field("Chất lượng", self.cb_quality,
                            "Không đăng nhập Bilibili thường chỉ tải được khoảng 360p–480p"))
        self.chk_cover = QCheckBox("Tải kèm ảnh bìa (.jpg)")
        sv.addWidget(self.chk_cover)
        self.q_host = QWidget(); self.q_lay = QVBoxLayout(self.q_host)
        self.q_lay.setContentsMargins(0, 0, 0, 0); self.q_lay.setSpacing(8); self.q_lay.addStretch(1)
        self.q_empty = QLabel("Chưa có phim nào. Bấm “⬇ Tải” ở một kết quả."); self.q_empty.setObjectName("hint")
        self.q_empty.setWordWrap(True)
        self.q_lay.insertWidget(0, self.q_empty)
        qs = QScrollArea(); qs.setWidgetResizable(True); qs.setWidget(self.q_host)
        qs.setFrameShape(QFrame.NoFrame); qs.setObjectName("scroll")
        qs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sv.addWidget(qs, 1)
        self.lb_qsum = QLabel(""); self.lb_qsum.setObjectName("stage")
        sv.addWidget(self.lb_qsum)
        b1 = _btn("Mở thư mục", "ghost"); b2 = _btn("■ Dừng", "danger", "Dừng phim đang tải và bỏ các phim đang chờ"); b3 = _btn("Xóa mục xong", "link")
        brow = QHBoxLayout(); brow.setSpacing(6); brow.addWidget(b1); brow.addWidget(b2); brow.addStretch(1); brow.addWidget(b3)
        sv.addLayout(brow)

        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addWidget(res, 1); cols.addWidget(side)
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addWidget(top); root.addLayout(cols, 1)

        # ---- sự kiện
        self.btn_go.clicked.connect(lambda: self._search(1))
        self.ed_q.returnPressed.connect(lambda: self._search(1))
        self.btn_vi.clicked.connect(self._vi_to_zh)
        self.btn_link.clicked.connect(self._from_link)
        self.ed_link.returnPressed.connect(self._from_link)
        self.btn_prev.clicked.connect(lambda: self._search(self.page - 1))
        self.btn_next.clicked.connect(lambda: self._search(self.page + 1))
        self.btn_out.clicked.connect(self._pick_out)
        b1.clicked.connect(self._open_out)
        b2.clicked.connect(self.dthread.stop_all)
        b3.clicked.connect(self._clear_done)
        for w in (self.cb_order, self.cb_dur):
            w.currentIndexChanged.connect(lambda _=0: self.keyword and self._search(1))
        self._update_pager()

    # ------------------------------------------------------------ tìm kiếm
    def _search(self, page: int):
        kw = self.ed_q.text().strip()
        if not kw:
            self.ed_q.setFocus()
            return
        if self._search_job and self._search_job.isRunning():
            return
        page = max(1, min(page, self.num_pages if kw == self.keyword else 50))
        self.keyword = kw
        order = ORDERS[self.cb_order.currentText()]
        dur = DURATIONS[self.cb_dur.currentText()]
        self.lb_count.setText("đang tìm…")
        self.btn_go.setEnabled(False)
        self._search_job = _Job(lambda: self.bili.search(kw, page, order, dur))
        self._search_job.result.connect(self._on_results)
        self._search_job.start()

    def _on_results(self, res, err):
        self.btn_go.setEnabled(True)
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
        self.lb_count.setText(f"“{self.keyword}” · khoảng {res.total:,} video".replace(",", "."))
        if not self.cards:
            self._show_empty("Không tìm thấy video nào. Thử từ khóa khác.")
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
        self.btn_prev.setEnabled(self.page > 1 and bool(self.keyword))
        self.btn_next.setEnabled(self.page < self.num_pages and bool(self.keyword))

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
        self.btn_link.setEnabled(False)
        self.lb_count.setText("đang đọc link…")

        def work():
            bvid, p = self.bili.resolve(text)
            meta = self.bili.video_meta(bvid)
            return bvid, p, meta
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
                                      self.cb_quality.currentText(), cover=self.chk_cover.isChecked()))
            self.lb_count.setText(f"Đã thêm vào hàng chờ: {name}")
            return
        self._ask_download(v)

    def _ask_download(self, v: Video):
        self.lb_count.setText("đang lấy danh sách tập…")
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
            self._enqueue(DownloadJob(p.url, name, out, self.cb_quality.currentText(), cover=self.chk_cover.isChecked()))
        self._store()

    def _enqueue(self, job: DownloadJob):
        row = JobRow(job)
        self.rows[id(job)] = row
        self.q_empty.hide()
        self.q_lay.insertWidget(self.q_lay.count() - 1, row)
        self.dthread.add(job)
        self._summary()

    def _on_job(self, job: DownloadJob):
        row = self.rows.get(id(job))
        if row:
            row.refresh()
        if job.state == "done":
            self.main_log(f"✔ Đã tải: {job.file}")
        elif job.state == "error":
            self.main_log(f"✖ Tải lỗi ({job.title}): {job.info}")
        self._summary()

    def _summary(self):
        jobs = [r.job for r in self.rows.values()]
        if not jobs:
            self.lb_qsum.setText("")
            return
        c = {s: sum(1 for j in jobs if j.state == s) for s in ("queued", "running", "done", "error", "stopped")}
        parts = [f"{c['done']}/{len(jobs)} xong"]
        if c["running"]:
            parts.append("đang tải 1")
        if c["queued"]:
            parts.append(f"{c['queued']} đang chờ")
        if c["error"]:
            parts.append(f"{c['error']} lỗi")
        self.lb_qsum.setText(" · ".join(parts))

    def _clear_done(self):
        for k, r in list(self.rows.items()):
            if r.job.state in ("done", "error", "stopped"):
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
                              "translate_titles": self.chk_tr.isChecked(), "cover": self.chk_cover.isChecked()}

    def store_state(self):
        self._store()

    def busy(self) -> bool:
        return any(r.job.state in ("running", "queued") for r in self.rows.values())

    def shutdown(self):
        self._store()
        self.dthread.shutdown()
        self._img_pool.shutdown(wait=False, cancel_futures=True)
