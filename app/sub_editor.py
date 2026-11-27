"""Bảng sửa phụ đề (kiểu Subtitle Edit): bản dịch cạnh bản gốc, xem trước trên video,
tô màu câu cần xem (trống, còn chữ gốc, chưa dịch, đọc quá nhanh, chồng thời gian)."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import srt

VIDEO_EXT = (".mp4", ".mkv", ".flv", ".mov", ".webm", ".avi", ".ts", ".m4v")
CJK_LANGS = ("Tiếng Trung", "Tiếng Nhật", "Tiếng Hàn")
MAX_CPS = 20.0          # ký tự/giây: quá mức này người xem đọc không kịp
TIME_ONE = re.compile(r"^\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}$")
ERR_BG, WARN_BG = QColor("#3a1820"), QColor("#33280f")


def find_video(sub_path: str) -> str:
    """Video cùng tên với phụ đề: 'tap1.vi.srt' -> 'tap1.mp4' (cùng thư mục)."""
    p = Path(sub_path)
    stem = p.stem
    stems = [stem, stem.rsplit(".", 1)[0]] if "." in stem else [stem]
    for s in stems:
        for ext in VIDEO_EXT:
            c = p.with_name(s + ext)
            if c.exists():
                return str(c)
    return ""


def sec(t: str) -> float:
    return srt._ms(t) / 1000.0


def problems(text: str, src: str | None, start: str, end: str, next_start: str | None,
             target_lang: str = "Tiếng Việt") -> tuple[str, list[str]]:
    """('err' | 'warn' | '', các lý do)."""
    errs, warns = [], []
    t = (text or "").strip()
    dur = sec(end) - sec(start)
    if not t:
        errs.append("trống")
    elif target_lang not in CJK_LANGS and srt.has_cjk(t):
        errs.append("còn chữ gốc")
    elif src and t == src.strip() and srt.has_cjk(src):
        errs.append("chưa dịch")
    if dur <= 0:
        errs.append("thời gian sai")
    elif t and len(t.replace("\n", "")) / dur > MAX_CPS:
        warns.append(f"đọc nhanh {len(t.replace(chr(10), '')) / dur:.0f} ký tự/giây")
    if next_start and sec(end) > sec(next_start) + 0.001:
        warns.append("chồng câu sau")
    reasons = errs + warns
    return ("err" if errs else "warn" if warns else ""), reasons


class _Preview(QThread):
    done = Signal(int, str, str)     # mã yêu cầu, đường dẫn ảnh, lỗi

    def __init__(self, token, fn):
        super().__init__()
        self.token, self.fn = token, fn

    def run(self):
        try:
            self.done.emit(self.token, self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(self.token, "", str(e))


class SubtitleEditor(QDialog):
    saved = Signal(str)
    COLS = ["#", "Bắt đầu", "Kết thúc", "Gốc", "Bản dịch", "Ký tự/giây"]

    def __init__(self, path: str, source_path: str = "", video: str = "", style=None,
                 target_lang: str = "Tiếng Việt", parent=None):
        super().__init__(parent)
        from .render_page import PreviewLabel
        self.path, self.video, self.style, self.target = path, video, style, target_lang
        self.cues = srt.parse(srt.read_text(path))
        src = srt.parse(srt.read_text(source_path)) if source_path and os.path.exists(source_path) else []
        # ghép bản gốc theo thứ tự câu (bản dịch giữ nguyên số câu và thời gian của bản gốc)
        self.src = [c.text for c in src] if len(src) == len(self.cues) else []
        self.has_src = bool(self.src)
        self.dirty, self._loading, self._token, self._info = False, False, 0, None
        self._jobs: list[_Preview] = []
        self._tmp = tempfile.mkdtemp(prefix="st_edit_")
        self.setWindowTitle(f"Sửa phụ đề · {Path(path).name}")
        self.resize(1240, 760)

        # ---- thanh trên
        self.lb_sum = QLabel(); self.lb_sum.setObjectName("stage")
        self.cb_filter = QComboBox(); self.cb_filter.addItems(["Tất cả câu", "Chỉ câu cần xem"])
        self.btn_next = QPushButton("Câu cần xem tiếp ▸"); self.btn_next.setObjectName("ghost")
        top = QHBoxLayout(); top.addWidget(self.lb_sum, 1); top.addWidget(self.cb_filter); top.addWidget(self.btn_next)

        # ---- bảng
        self.table = QTableWidget(len(self.cues), len(self.COLS)); self.table.setObjectName("grid")
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        hh = self.table.horizontalHeader()
        for c, m in enumerate((QHeaderView.ResizeToContents, QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                               QHeaderView.Stretch, QHeaderView.Stretch, QHeaderView.ResizeToContents)):
            hh.setSectionResizeMode(c, m)
        self.table.setColumnHidden(3, not self.has_src)

        # ---- bên phải: xem trước + sửa câu đang chọn
        self.preview = PreviewLabel()
        self.preview.setText("Không có video cùng tên để xem trước" if not video else "Chọn một câu để xem trước")
        self.lb_time = QLabel(""); self.lb_time.setObjectName("hint")
        self.lb_src = QLabel(""); self.lb_src.setObjectName("muted"); self.lb_src.setWordWrap(True)
        self.lb_src.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.ed_text = QPlainTextEdit(); self.ed_text.setMaximumHeight(90)
        self.ed_text.setPlaceholderText("Chọn một câu trong bảng để sửa")
        self.lb_why = QLabel(""); self.lb_why.setObjectName("hint"); self.lb_why.setWordWrap(True)
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(8)
        rv.addWidget(self.preview, 1); rv.addWidget(self.lb_time)
        if self.has_src:
            rv.addWidget(self.lb_src)
        rv.addWidget(self.ed_text); rv.addWidget(self.lb_why)
        split = QSplitter(Qt.Horizontal); split.addWidget(self.table); split.addWidget(right)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)

        # ---- đáy
        legend = QLabel(f"<span style='background:{ERR_BG.name()}'>&nbsp;đỏ&nbsp;</span> trống / còn chữ gốc / chưa dịch / "
                        f"thời gian sai &nbsp; <span style='background:{WARN_BG.name()}'>&nbsp;vàng&nbsp;</span> "
                        f"đọc quá nhanh (&gt;{MAX_CPS:.0f} ký tự/giây) / chồng câu sau · bấm đúp ô để sửa giờ hoặc chữ")
        legend.setObjectName("hint"); legend.setTextFormat(Qt.RichText)
        self.btn_save = QPushButton("Lưu"); self.btn_save.setObjectName("primary")
        b_close = QPushButton("Đóng"); b_close.setObjectName("ghost")
        bot = QHBoxLayout(); bot.addWidget(legend, 1); bot.addWidget(b_close); bot.addWidget(self.btn_save)

        v = QVBoxLayout(self); v.setSpacing(10)
        v.addLayout(top); v.addWidget(split, 1); v.addLayout(bot)

        self._timer = QTimer(self); self._timer.setSingleShot(True); self._timer.setInterval(250)
        self._timer.timeout.connect(self._render_preview)
        self._fill()
        self.table.itemChanged.connect(self._on_item)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.ed_text.textChanged.connect(self._on_text)
        self.cb_filter.currentIndexChanged.connect(lambda _=0: self._apply_filter())
        self.btn_next.clicked.connect(self._next_problem)
        self.btn_save.clicked.connect(self.save)
        b_close.clicked.connect(self.reject)
        if self.cues:
            self.table.selectRow(0)

    # ------------------------------------------------------------ bảng
    def _fill(self):
        self._loading = True
        for r, c in enumerate(self.cues):
            vals = [str(r + 1), c.start, c.end, self.src[r] if self.has_src else "", c.text, ""]
            for col, val in enumerate(vals):
                it = QTableWidgetItem(val.replace("\n", " ⏎ ") if col in (3, 4) else val)
                if col in (0, 3, 5):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if col in (0, 1, 2, 5):
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, col, it)
        self._loading = False
        self._refresh_all()

    def _row_state(self, r: int) -> tuple[str, list[str]]:
        c = self.cues[r]
        nxt = self.cues[r + 1].start if r + 1 < len(self.cues) else None
        return problems(c.text, self.src[r] if self.has_src else None, c.start, c.end, nxt, self.target)

    def _paint_row(self, r: int):
        state, why = self._row_state(r)
        c = self.cues[r]
        dur = sec(c.end) - sec(c.start)
        self._loading = True
        cps = self.table.item(r, 5)
        cps.setText(f"{len(c.text.replace(chr(10), '')) / dur:.0f}" if dur > 0 else "—")
        bg = ERR_BG if state == "err" else WARN_BG if state == "warn" else None
        for col in range(len(self.COLS)):
            it = self.table.item(r, col)
            it.setBackground(bg if bg else QColor(0, 0, 0, 0))
            it.setToolTip(", ".join(why))
        self._loading = False

    def _refresh_all(self):
        for r in range(len(self.cues)):
            self._paint_row(r)
        self._summary()
        self._apply_filter()

    def _problem_rows(self) -> list[int]:
        return [r for r in range(len(self.cues)) if self._row_state(r)[0]]

    def _summary(self):
        bad = self._problem_rows()
        n_err = sum(1 for r in bad if self._row_state(r)[0] == "err")
        self.lb_sum.setText(f"{len(self.cues)} câu · " + (f"{len(bad)} câu cần xem ({n_err} lỗi)" if bad else "✔ không có câu nào cần xem")
                            + (" · chưa lưu" if self.dirty else ""))
        self.btn_next.setEnabled(bool(bad))

    def _apply_filter(self):
        only = self.cb_filter.currentIndex() == 1
        for r in range(len(self.cues)):
            self.table.setRowHidden(r, only and not self._row_state(r)[0])

    def _next_problem(self):
        bad = self._problem_rows()
        if not bad:
            return
        cur = self.table.currentRow()
        r = next((x for x in bad if x > cur), bad[0])
        self.table.selectRow(r)
        self.table.scrollToItem(self.table.item(r, 0))

    # ------------------------------------------------------------ sửa
    def _on_item(self, it: QTableWidgetItem):
        if self._loading:
            return
        r, col = it.row(), it.column()
        c = self.cues[r]
        if col in (1, 2):
            val = it.text().strip()
            if not TIME_ONE.match(val):
                self._loading = True
                it.setText(c.start if col == 1 else c.end)
                self._loading = False
                self.lb_why.setText("✖ Giờ phải có dạng 00:01:02,345")
                return
            val = srt._norm_time(val)
            if col == 1:
                c.start = val
            else:
                c.end = val
            self._loading = True; it.setText(val); self._loading = False
        elif col == 4:
            c.text = it.text().replace(" ⏎ ", "\n")
            if r == self.table.currentRow():
                self.ed_text.blockSignals(True); self.ed_text.setPlainText(c.text); self.ed_text.blockSignals(False)
        self._changed(r)

    def _on_text(self):
        r = self.table.currentRow()
        if not 0 <= r < len(self.cues):
            return
        self.cues[r].text = self.ed_text.toPlainText().strip()
        self._loading = True
        self.table.item(r, 4).setText(self.cues[r].text.replace("\n", " ⏎ "))
        self._loading = False
        self._changed(r)

    def _changed(self, r: int):
        self.dirty = True
        for x in (r - 1, r):
            if 0 <= x < len(self.cues):
                self._paint_row(x)
        self._summary()
        self._show_why(r)
        self._timer.start()

    def _on_select(self):
        r = self.table.currentRow()
        if not 0 <= r < len(self.cues):
            return
        c = self.cues[r]
        self.ed_text.blockSignals(True); self.ed_text.setPlainText(c.text); self.ed_text.blockSignals(False)
        self.lb_time.setText(f"Câu {r + 1}/{len(self.cues)} · {c.start} → {c.end} · dài {sec(c.end) - sec(c.start):.2f} giây")
        if self.has_src:
            self.lb_src.setText("Gốc: " + self.src[r])
        self._show_why(r)
        self._timer.start()

    def _show_why(self, r: int):
        state, why = self._row_state(r)
        self.lb_why.setText(("⚠ " + ", ".join(why)) if why else "✔ Câu này ổn")
        self.lb_why.setStyleSheet("color:#ef5d6c" if state == "err" else "color:#f2b84b" if state == "warn" else "")

    # ------------------------------------------------------------ xem trước
    def _render_preview(self):
        r = self.table.currentRow()
        if not self.video or not 0 <= r < len(self.cues):
            return
        from .render import Cover, SubStyle, preview_frame, probe
        c = self.cues[r]
        at = (sec(c.start) + sec(c.end)) / 2
        cues = [srt.Cue(x.index, x.start, x.end, x.text, x.extra) for x in self.cues]
        style = self.style or SubStyle()
        self._token += 1
        out = os.path.join(self._tmp, f"p{self._token}.png")
        video = self.video

        def work():
            if self._info is None:
                self._info = probe(video)
            return preview_frame(video, cues, style, Cover(), at, out, self._info, max_w=960)
        job = _Preview(self._token, work)
        job.done.connect(self._on_preview)
        job.finished.connect(lambda j=job: j in self._jobs and self._jobs.remove(j))
        self._jobs.append(job)
        job.start()

    def _on_preview(self, token: int, path: str, err: str):
        if token != self._token:
            return
        if err:
            self.preview.setText(f"Không xem trước được: {err[:200]}")
        else:
            self.preview.set_image(path)

    # ------------------------------------------------------------ lưu / đóng
    def save(self) -> bool:
        try:
            bak = self.path + ".bak"
            if not os.path.exists(bak):
                shutil.copyfile(self.path, bak)
            with open(self.path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(srt.compose(self.cues))
        except OSError as e:
            QMessageBox.warning(self, "Lưu phụ đề", f"Không lưu được: {e}")
            return False
        self.dirty = False
        self._summary()
        self.saved.emit(self.path)
        return True

    def reject(self):
        """Đóng (nút Đóng, Esc, dấu ×): hỏi lưu nếu còn thay đổi."""
        if self.dirty:
            ans = QMessageBox.question(self, "Chưa lưu", "Lưu các thay đổi trước khi đóng?",
                                       QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel or (ans == QMessageBox.Save and not self.save()):
                return
        self._timer.stop()
        for j in list(self._jobs):
            j.wait(5000)
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().reject()
