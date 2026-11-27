"""Tab Tự động từ link (kiểu Dudu Reup): dán link → tải video (kèm phụ đề có sẵn) → nghe giọng nếu cần
→ dịch → in phụ đề & xuất MP4. Mỗi bước hiện trên một danh sách có dấu ✔ và thời gian chạy.

Dùng lại nguyên các phần của app: Downloader (tab Tìm & tải), Worker (tab Dịch), Exporter (tab Xuất video)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QVBoxLayout, QWidget,
)

from . import srt, theme
from .bilibili import QUALITIES, DownloadJob, Downloader
from .sources import SITES, extract

STEPS = [
    ("download", "Tải video", "bản tốt nhất khách được xem, kèm phụ đề có sẵn"),
    ("subs", "Phụ đề có sẵn", "phụ đề Bilibili (cần đăng nhập) — có thì khỏi nghe giọng"),
    ("listen", "Nghe giọng thành phụ đề", "AI nghe tiếng trong video, ghi ra .zh.srt"),
    ("translate", "Dịch sang tiếng Việt", "theo AI, ngôn ngữ và prompt ở tab Dịch"),
    ("export", "In phụ đề & xuất MP4", "theo kiểu chữ, che chữ, khung hình ở tab Xuất video"),
]
RESULTS = ["Video Vietsub (in phụ đề vào video)", "Chỉ phụ đề tiếng Việt (.vi.srt)"]


class _Stopped(Exception):
    pass


# ====================================================================== luồng chạy cả quy trình
class AutoWorker(QThread):
    step = Signal(str, str, str)       # bước, trạng thái (wait|now|done|skip|err), ghi chú
    log = Signal(str)
    progress = Signal(float)           # % của bước đang chạy
    done = Signal(dict, str)           # kết quả {video, src, vi, out}, lỗi ("" | "stopped" | thông báo)

    def __init__(self, link: str, bili, out_dir: str, quality: str, providers, settings, asr: str,
                 export: dict | None):
        super().__init__()
        self.link, self.bili, self.out_dir, self.quality = link, bili, out_dir, quality
        self.providers, self.settings, self.asr, self.export = providers, settings, asr, export
        self._stop = threading.Event()
        self._dl: Downloader | None = None
        self._w = None
        self._ex = None
        self._listen = "wait"

    def stop(self):
        self._stop.set()
        if self._dl:
            self._dl.stop_event.set()
        if self._w:
            self._w.stop()
        if self._ex:
            self._ex.stop()

    def _check(self):
        if self._stop.is_set():
            raise _Stopped()

    # ------------------------------------------------------------
    def run(self):
        res: dict = {}
        try:
            video = self._download(res)
            self._check()
            self._subs(video)
            self._check()
            vi = self._translate(video, res)
            self._check()
            if self.export is None:
                self.step.emit("export", "skip", "chỉ cần phụ đề")
            else:
                self._export(video, vi, res)
            self.done.emit(res, "")
        except _Stopped:
            self.done.emit(res, "stopped")
        except Exception as e:  # noqa: BLE001
            self.done.emit(res, str(e) or type(e).__name__)

    def _download(self, res: dict) -> str:
        self.step.emit("download", "now", "đang đọc link…")
        url, site = extract(self.link)
        if site == "Bilibili":
            bvid, p = self.bili.resolve(url)
            meta = self.bili.video_meta(bvid)
            title = meta.get("title") or bvid
            pages = meta.get("pages") or []
            if len(pages) > 1:
                p = p or 1
                part = next((x for x in pages if x.get("page") == p), {})
                title = f"{title} - P{p} {part.get('part', '')}".strip()
                if "?p=" not in url:
                    self.log.emit(f"  Video có {len(pages)} phần, tự động lấy phần {p}. "
                                  "Muốn phần khác thì dán link có ?p=số phần.")
            dl_url = f"{self.bili.web}/video/{bvid}" + (f"?p={p}" if p else "")
            job = DownloadJob(dl_url, title, self.out_dir, self.quality, subs=True)
        else:
            job = DownloadJob(url, f"{site}: {url[:60]}", os.path.join(self.out_dir, site.split(" (")[0]),
                              self.quality, auto_title=True)
        self._dl = Downloader(on_update=self._on_dl, log=self.log.emit, bili=self.bili)
        if self._stop.is_set():
            raise _Stopped()
        self._dl.run(job)
        if job.state == "stopped" or self._stop.is_set():
            raise _Stopped()
        if job.state != "done" or not job.file:
            raise RuntimeError(f"Tải video lỗi: {job.info}")
        res["video"] = job.file
        self.step.emit("download", "done", Path(job.file).name + (f" · {job.info.split(' · ', 2)[2]}"
                                                                   if job.info.count(" · ") >= 2 else ""))
        return job.file

    def _on_dl(self, job: DownloadJob):
        if job.state == "running":
            self.progress.emit(job.pct)
            self.step.emit("download", "now", job.info)

    def _subs(self, video: str):
        from .gui import source_srt_for
        src = source_srt_for(video, "", self.settings.source_lang)
        if os.path.exists(src):
            n = len(srt.parse(srt.read_text(src)))
            self.step.emit("subs", "done", f"có sẵn {n} câu → {Path(src).name}")
        else:
            login = getattr(self.bili, "logged_in", lambda: False)()
            self.step.emit("subs", "skip", "video không có phụ đề có sẵn" if login else
                           "chưa đăng nhập Bilibili nên không lấy được (đăng nhập ở tab Tìm & tải)")

    def _translate(self, video: str, res: dict) -> str:
        from .gui import Worker, output_path_for, source_srt_for
        src = source_srt_for(video, "", self.settings.source_lang)
        vi = output_path_for(video, "", self.settings.target_lang)
        if os.path.exists(vi) and os.path.exists(src) and os.path.getmtime(vi) >= os.path.getmtime(src):
            n_vi, n_src = len(srt.parse(srt.read_text(vi))), len(srt.parse(srt.read_text(src)))
            if n_vi and n_vi == n_src:     # bản dịch đủ câu của lần trước: dùng lại, không tốn lượt AI
                self.step.emit("listen", "skip", "dùng phụ đề gốc đã có")
                self.step.emit("translate", "done", f"dùng lại bản dịch đã có ({n_vi} câu) · muốn dịch lại thì xoá {Path(vi).name}")
                self.log.emit(f"  Dùng lại bản dịch đã có: {Path(vi).name}")
                res["vi"] = vi
                return vi
        w = Worker("translate", [video], "", self.providers, self.settings)
        w.asr_engine = self.asr
        self._w = w
        if self._stop.is_set():
            raise _Stopped()
        msg = {}
        w.log.connect(self.log.emit, Qt.DirectConnection)
        w.event.connect(self._on_event, Qt.DirectConnection)
        w.progress.connect(lambda d, t: self.progress.emit(d * 100.0 / t if t else 0), Qt.DirectConnection)
        w.done.connect(lambda m: msg.setdefault("m", m), Qt.DirectConnection)
        self._listen = "wait"
        w.run()                            # chạy ngay trong luồng này (không start luồng mới)
        if self._stop.is_set() or msg.get("m") == "Đã dừng.":
            raise _Stopped()
        vi = output_path_for(video, "", self.settings.target_lang)
        if not os.path.exists(vi) or os.path.getmtime(vi) < os.path.getmtime(video):
            raise RuntimeError(msg.get("m") or "Không dịch được phụ đề")
        if self._listen == "now":
            self.step.emit("listen", "done", "")
        elif self._listen == "wait":
            self.step.emit("listen", "skip", "dùng phụ đề có sẵn")
        res["vi"] = vi
        n = len(srt.parse(srt.read_text(vi)))
        self.step.emit("translate", "done", f"{n} câu → {Path(vi).name}")
        return vi

    def _on_event(self, e: dict):
        if e.get("type") != "stage":
            return
        st = e.get("stage")
        if st in ("audio", "listen"):
            self._listen = "now"
            self.step.emit("listen", "now", e.get("text", ""))
        elif st in ("translate", "fix", "save"):
            if self._listen == "now":
                self.step.emit("listen", "done", "")
                self._listen = "done"
            elif self._listen == "wait":
                self.step.emit("listen", "skip", "dùng phụ đề có sẵn")
                self._listen = "skip"
            self.step.emit("translate", "now", e.get("text", ""))

    def _export(self, video: str, vi: str, res: dict):
        from .render import Exporter
        self.step.emit("export", "now", "đang in phụ đề vào video…")
        out = str(Path(video).with_name(Path(video).stem + ".vietsub.mp4"))
        cues = srt.parse(srt.read_text(vi))
        ex = Exporter(video, cues, self.export["style"], self.export["cover"], self.export["opts"], out,
                      progress=lambda p, s: (self.progress.emit(p), self.step.emit("export", "now", f"tốc độ {s}" if s else "")),
                      log=self.log.emit, look=self.export["look"])
        self._ex = ex
        if self._stop.is_set():
            raise _Stopped()
        r = ex.run()
        if r == "stopped" or self._stop.is_set():
            raise _Stopped()
        res["out"] = r or out
        self.step.emit("export", "done", Path(res["out"]).name)


# ====================================================================== một dòng bước
class StepRow(QFrame):
    ICON = {"wait": ("", "#3a465c"), "now": ("sync", theme.ACCENT_HOVER), "done": ("check", theme.ACCENT),
            "skip": ("", "#4a5568"), "err": ("error", theme.DANGER)}

    def __init__(self, name: str, hint: str):
        super().__init__()
        self.setObjectName("stepRow")
        h = QHBoxLayout(self); h.setContentsMargins(10, 8, 10, 8); h.setSpacing(12)
        self.ic = QLabel(); self.ic.setFixedSize(22, 22); self.ic.setAlignment(Qt.AlignCenter)
        col = QVBoxLayout(); col.setSpacing(1)
        self.lb_name = QLabel(name); self.lb_name.setObjectName("stepName")
        self.lb_info = QLabel(hint); self.lb_info.setObjectName("hint")
        self.hint = hint
        col.addWidget(self.lb_name); col.addWidget(self.lb_info)
        self.lb_time = QLabel("–"); self.lb_time.setObjectName("stepTime")
        self.lb_time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.ic); h.addLayout(col, 1); h.addWidget(self.lb_time)
        self.state, self.t0, self.t1 = "", 0.0, 0.0
        self.set_state("wait")

    def set_state(self, state: str, info: str = ""):
        if state != self.state:
            if state == "now":
                self.t0, self.t1 = time.monotonic(), 0.0
            elif self.state == "now":
                self.t1 = time.monotonic()
            self.state = state
            glyph, color = self.ICON[state]
            if glyph:
                self.ic.setPixmap(theme.glyph_pixmap(glyph, color, 18))
            else:
                self.ic.clear()
                self.ic.setText("○" if state == "wait" else "–")
                self.ic.setStyleSheet(f"color:{color}; font-size:13pt;")
            for w in (self, self.lb_name):
                w.setProperty("state", state)
                w.style().unpolish(w); w.style().polish(w)
            if state == "wait":
                self.lb_time.setText("–")
        if info or state == "wait":
            fm = self.lb_info.fontMetrics()
            text = info or self.hint
            self.lb_info.setText(fm.elidedText(text, Qt.ElideMiddle, max(120, self.lb_info.width() or 360)))
            self.lb_info.setToolTip(text)
        self.tick()

    def tick(self):
        if self.state == "now" and self.t0:
            self.lb_time.setText(_dur(time.monotonic() - self.t0))
        elif self.state in ("done", "err") and self.t0:
            self.lb_time.setText(_dur((self.t1 or time.monotonic()) - self.t0))
        elif self.state == "skip":
            self.lb_time.setText("bỏ qua")


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s}s"


# ====================================================================== trang
class AutoPage(QWidget):
    def __init__(self, main):
        super().__init__()
        from .gui import btn, card, field
        self.main = main
        self.cfg = main.cfg
        self.worker: AutoWorker | None = None
        self.result: dict = {}
        st = self.cfg.get("auto", {})

        # ---- 1 · Link
        c1, v1 = card("Dán link video", "1")
        self.ed_link = QLineEdit(); self.ed_link.setObjectName("searchBox")
        self.ed_link.setPlaceholderText("Link Bilibili, Douyin, Tây Qua, Weibo… hoặc cả đoạn chữ chia sẻ")
        self.btn_paste = btn("Dán", "ghost", "Dán link vừa sao chép")
        lrow = QHBoxLayout(); lrow.setSpacing(8); lrow.addWidget(self.ed_link, 1); lrow.addWidget(self.btn_paste)
        sites = QLabel("Hỗ trợ: " + ", ".join(s.split(" (")[0] for s in SITES) +
                       ". Video Bilibili nhiều phần: dán link có ?p=số phần để chọn phần.")
        sites.setObjectName("hint"); sites.setWordWrap(True)
        v1.addLayout(lrow); v1.addWidget(sites)

        # ---- 2 · Kết quả
        c2, v2 = card("Muốn nhận được", "2")
        self.cb_result = QComboBox(); self.cb_result.addItems(RESULTS)
        self.cb_quality = QComboBox(); self.cb_quality.addItems([q for q in QUALITIES if not q.startswith("Chỉ âm thanh")])
        qrow = QHBoxLayout(); qrow.setSpacing(10)
        qrow.addLayout(field("Kết quả", self.cb_result), 3); qrow.addLayout(field("Chất lượng tải", self.cb_quality), 2)
        self.ed_out = QLineEdit(); self.ed_out.setPlaceholderText("Thư mục lưu phim")
        b_out = btn("Chọn…", "ghost"); b_out.clicked.connect(self._pick_out)
        orow = QHBoxLayout(); orow.setSpacing(8); orow.addWidget(self.ed_out, 1); orow.addWidget(b_out)
        v2.addLayout(qrow)
        v2.addLayout(field("Lưu vào thư mục", orow))
        # các cài đặt lấy từ tab khác: hiện tóm tắt + nút đi tới
        self.lb_ai = QLabel(); self.lb_ai.setObjectName("hint"); self.lb_ai.setWordWrap(True)
        self.lb_ai.setTextFormat(Qt.RichText)
        self.lb_look = QLabel(); self.lb_look.setObjectName("hint"); self.lb_look.setWordWrap(True)
        b_tr = btn("Chỉnh cách dịch…", "link"); b_tr.clicked.connect(lambda: self.main._go_page(1))
        b_lk = btn("Chỉnh kiểu chữ…", "link"); b_lk.clicked.connect(lambda: self.main._go_page(3))
        r1 = QHBoxLayout(); r1.addWidget(self.lb_ai, 1); r1.addWidget(b_tr)
        r2 = QHBoxLayout(); r2.addWidget(self.lb_look, 1); r2.addWidget(b_lk)
        v2.addLayout(r1); v2.addLayout(r2)
        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(c1); left.addWidget(c2); left.addStretch(1)

        # ---- Tiến độ từng bước
        c3, v3 = card("Tiến độ")
        self.rows: dict[str, StepRow] = {}
        for key, name, hint in STEPS:
            r = StepRow(name, hint)
            self.rows[key] = r
            v3.addWidget(r)
        v3.addStretch(1)
        self.res_box = QFrame(); self.res_box.setObjectName("aiBar")
        rb = QVBoxLayout(self.res_box); rb.setContentsMargins(12, 10, 12, 10); rb.setSpacing(8)
        self.lb_res = QLabel(); self.lb_res.setWordWrap(True); self.lb_res.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_open = btn("Mở thư mục", "ghost")
        self.btn_edit = btn("Sửa phụ đề", "ghost", "Mở bảng sửa phụ đề vừa dịch")
        self.btn_render = btn("Mở ở tab Xuất video", "ghost", "Chỉnh kiểu chữ rồi xuất lại")
        brr = QHBoxLayout(); brr.setSpacing(8)
        for b in (self.btn_open, self.btn_edit, self.btn_render):
            brr.addWidget(b)
        brr.addStretch(1)
        rb.addWidget(self.lb_res); rb.addLayout(brr)
        self.res_box.hide()
        v3.addWidget(self.res_box)
        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addLayout(left, 5); cols.addWidget(c3, 6)

        # ---- thanh hành động
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 14, 18, 14); av.setSpacing(10)
        self.bar = QProgressBar(); self.bar.setFixedHeight(12)
        self.lb_status = QLabel("Dán link rồi bấm “Bắt đầu”. App làm hết các bước, bạn chỉ cần chờ.")
        self.lb_status.setObjectName("stage")
        self.lb_total = QLabel(""); self.lb_total.setObjectName("chip"); self.lb_total.hide()
        self.btn_stop = btn("■  Dừng", "danger"); self.btn_stop.setEnabled(False)
        self.btn_run = btn("Bắt đầu", "primary")
        self.btn_run.setIcon(theme.icon("bolt", "#04150f", 18))
        brow = QHBoxLayout(); brow.setSpacing(8)
        brow.addWidget(self.lb_status, 1); brow.addWidget(self.lb_total); brow.addWidget(self.btn_stop); brow.addWidget(self.btn_run)
        av.addWidget(self.bar); av.addLayout(brow)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addLayout(cols, 1); root.addWidget(act)

        self.cb_result.setCurrentIndex(max(0, RESULTS.index(st["result"])) if st.get("result") in RESULTS else 0)
        if st.get("quality") in QUALITIES:
            self.cb_quality.setCurrentText(st["quality"])
        self.ed_out.setText(st.get("out_dir", ""))
        self.btn_paste.clicked.connect(lambda: self.ed_link.setText(QGuiApplication.clipboard().text().strip()))
        self.ed_link.returnPressed.connect(self.start)
        self.btn_run.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_edit.clicked.connect(self._edit_subs)
        self.btn_render.clicked.connect(self._to_render)
        self.cb_result.currentIndexChanged.connect(lambda _=0: self._summaries())
        self.timer = QTimer(self); self.timer.setInterval(500); self.timer.timeout.connect(self._tick)
        self._t0 = 0.0

    # ------------------------------------------------------------ hiển thị
    def on_show(self):
        if not self.ed_out.text().strip():
            self.ed_out.setText(self.main.search_page.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili"))
        self._summaries()

    def _summaries(self):
        m = self.main
        src = [n.split(" (")[0] for n, k, _, _ in m._sources() if k]
        self.lb_ai.setText(f"Dịch: {m.cb_source.currentText()} → {m.cb_target.currentText()} · AI: "
                           + (", ".join(src) if src else "<span style='color:#f2b84b'>chưa có API key</span>")
                           + (f" · prompt “{m.cb_prompt.currentText()}”" if m.cb_prompt.currentIndex() > 0 else ""))
        rp = m.render_page
        video = self.cb_result.currentIndex() == 0
        try:
            st, cv = rp._style(), rp._cover()
            cover = {"none": "không che chữ cũ", "blur": "làm mờ chữ cũ", "box": "che hộp chữ cũ",
                     "erase": "xoá chữ cũ"}.get(cv.mode, cv.mode)
            look = f"Kiểu chữ: {st.font_family} {st.size_pct:.1f}% · {cover}"
        except Exception:  # noqa: BLE001
            look = "Kiểu chữ theo tab Xuất video"
        self.lb_look.setText(look if video else "Không xuất video, chỉ lưu phụ đề .vi.srt")

    def store_state(self):
        self.cfg["auto"] = {"result": self.cb_result.currentText(), "quality": self.cb_quality.currentText(),
                            "out_dir": self.ed_out.text().strip()}

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu phim", self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    # ------------------------------------------------------------ chạy
    def start(self):
        if self.busy():
            return
        from .providers import ProviderError
        link = self.ed_link.text().strip()
        if not link:
            return QMessageBox.warning(self, "Thiếu link", "Dán link video vào ô trên cùng.")
        try:
            extract(link)
        except ValueError as e:
            return QMessageBox.warning(self, "Link chưa đúng", str(e))
        m = self.main
        try:
            providers = m._make_translate_providers()
        except ProviderError as e:
            return QMessageBox.warning(self, "Chưa có AI", f"{e}\n\nVào Cài đặt AI để thêm key (có nhà cung cấp miễn phí).")
        out = self.ed_out.text().strip() or str(Path.home() / "Videos" / "Bilibili")
        export = None
        if self.cb_result.currentIndex() == 0:
            rp = m.render_page
            export = {"style": rp._style(), "cover": rp._cover(), "opts": replace(rp._opts(), audio_file=""),
                      "look": rp._look()}
        self.store_state(); m._save_cfg()
        self.result = {}
        self.res_box.hide()
        for r in self.rows.values():
            r.set_state("wait")
        self.worker = AutoWorker(link, m.search_page.bili, out, self.cb_quality.currentText(), providers,
                                 m.translate_settings(), m.cb_asr.currentData() or "", export)
        self.worker.step.connect(self._on_step)
        self.worker.log.connect(m._log)
        self.worker.progress.connect(lambda p: self.bar.setValue(int(p)))
        self.worker.done.connect(self._on_done)
        self._running(True)
        self._t0 = time.monotonic()
        self.timer.start()
        m._log(f"━━ Tự động từ link: {link}")
        self.worker.start()

    def stop(self):
        if self.busy():
            self.worker.stop()
            self.lb_status.setText("Đang dừng…")

    def busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def shutdown(self):
        if self.busy():
            self.worker.stop()
            self.worker.wait(8000)
        self.store_state()

    def _running(self, on: bool):
        self.btn_run.setEnabled(not on)
        self.btn_stop.setEnabled(on)
        self.ed_link.setEnabled(not on)
        if on:
            self.bar.setValue(0)

    def _on_step(self, key: str, state: str, info: str):
        row = self.rows[key]
        if state == "now" and row.state != "now":
            self.bar.setValue(0)
        row.set_state(state, info)
        if state == "now":
            name = next(n for k, n, _ in STEPS if k == key)
            self.lb_status.setText(f"{name}: {info}" if info else name)

    def _tick(self):
        for r in self.rows.values():
            r.tick()
        self.lb_total.setText("⏱ " + _dur(time.monotonic() - self._t0))
        self.lb_total.show()

    def _on_done(self, res: dict, err: str):
        self.timer.stop(); self._tick()
        self._running(False)
        self.result = res
        for r in self.rows.values():
            if r.state == "now":
                r.set_state("err" if err and err != "stopped" else "wait", r.lb_info.toolTip())
        m = self.main
        if err == "stopped":
            self.lb_status.setText("Đã dừng. Bấm Bắt đầu để chạy lại: phần đã tải, đã nghe, đã dịch được dùng lại.")
            m._log("  ■ Đã dừng chế độ tự động")
        elif err:
            self.lb_status.setText(f"✖ {err}")
            self.lb_status.setToolTip(err)
            m._log(f"  ✖ {err}")
        else:
            self.bar.setValue(100)
            out = res.get("out") or res.get("vi", "")
            self.lb_status.setText(f"✔ Xong sau {_dur(time.monotonic() - self._t0)}")
            m._log(f"  ✔ Tự động xong → {out}")
        if res.get("video"):
            lines = [f"<b>Video:</b> {Path(res['video']).name}"]
            if res.get("vi"):
                lines.append(f"<b>Phụ đề:</b> {Path(res['vi']).name}")
            if res.get("out"):
                lines.append(f"<b>Video Vietsub:</b> {Path(res['out']).name}")
            self.lb_res.setText("<br>".join(lines))
            self.btn_edit.setEnabled(bool(res.get("vi")))
            self.btn_render.setEnabled(bool(res.get("video")))
            self.res_box.show()

    # ------------------------------------------------------------ sau khi xong
    def _open_folder(self):
        f = self.result.get("out") or self.result.get("vi") or self.result.get("video")
        if not f:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(f)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(f)])

    def _edit_subs(self):
        from .gui import source_srt_for
        from .sub_editor import SubtitleEditor
        vi, video = self.result.get("vi"), self.result.get("video", "")
        if not vi:
            return
        src = source_srt_for(video, "", self.main.cb_source.currentText()) if video else ""
        dlg = SubtitleEditor(vi, src if src and os.path.exists(src) else "", video,
                             self.main.render_page._style(), self.main.cb_target.currentText(), self)
        dlg.saved.connect(lambda p: self.main._log(f"✔ Đã lưu phụ đề đã sửa: {p}"))
        dlg.exec()

    def _to_render(self):
        video = self.result.get("video")
        if not video:
            return
        rp = self.main.render_page
        rp.set_video(video, self.result.get("vi"))
        self.main._go_page(3)
