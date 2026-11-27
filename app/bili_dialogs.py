"""Hộp thoại Bilibili: đăng nhập bằng mã QR, lấy phụ đề có sẵn (SRT / VTT / TXT / JSON)."""
from __future__ import annotations

import io
import json
import os

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .bilibili import Bili, BiliError, Part, SubTrack, _safe_name, _ts, subs_to_srt, subs_to_txt, subs_to_vtt


class _Run(QThread):
    result = Signal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit(None, str(e))


def _btn(text, kind=None, tip=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def _keep(dlg, job: _Run):
    """Giữ tham chiếu luồng tới khi chạy xong (đóng hộp thoại giữa chừng không làm hỏng luồng)."""
    dlg._jobs.append(job)
    job.finished.connect(lambda: job in dlg._jobs and dlg._jobs.remove(job))
    job.start()


_ORPHANS: list = []


def _release(dlg):
    """Đóng hộp thoại khi luồng còn chạy: tách luồng khỏi hộp thoại, để nó tự chạy xong ở nền."""
    for j in list(dlg._jobs):
        try:
            j.result.disconnect()
        except (RuntimeError, TypeError):
            pass
        j.setParent(None)
        _ORPHANS.append(j)
        j.finished.connect(lambda j=j: j in _ORPHANS and _ORPHANS.remove(j))
    dlg._jobs.clear()


def qr_png(text: str) -> bytes:
    import segno   # thư viện nhỏ, thuần Python (run.bat tự cài)
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=7, border=3, dark="#000000", light="#ffffff")
    return buf.getvalue()


# ====================================================================== đăng nhập
class LoginDialog(QDialog):
    """Quét mã QR bằng app Bilibili trên điện thoại (Tôi › biểu tượng quét). Thành công -> accept()."""

    def __init__(self, bili: Bili, parent=None):
        super().__init__(parent)
        self.bili, self._jobs, self._key, self._polling = bili, [], "", False
        self.setWindowTitle("Đăng nhập Bilibili")
        self.setMinimumWidth(420)
        v = QVBoxLayout(self); v.setSpacing(10)
        intro = QLabel("Mở app <b>Bilibili</b> trên điện thoại → nút quét mã (góc trên) → quét mã bên dưới → bấm xác nhận.<br>"
                       "Đăng nhập để lấy được <b>phụ đề</b> (kể cả phụ đề AI) và chất lượng cao hơn nếu tài khoản có VIP.")
        intro.setWordWrap(True)
        self.qr = QLabel("đang lấy mã…"); self.qr.setAlignment(Qt.AlignCenter); self.qr.setMinimumSize(260, 260)
        self.status = QLabel(""); self.status.setObjectName("hint"); self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.btn_new = _btn("↻ Lấy mã mới", "ghost")
        self.btn_new.clicked.connect(self._start)
        self.ed_sess = QLineEdit(); self.ed_sess.setEchoMode(QLineEdit.Password)
        self.ed_sess.setPlaceholderText("Hoặc dán cookie SESSDATA (lấy từ trình duyệt đã đăng nhập bilibili.com)")
        self.btn_sess = _btn("Dùng SESSDATA", "ghost")
        self.btn_sess.clicked.connect(self._use_sess)
        srow = QHBoxLayout(); srow.addWidget(self.ed_sess, 1); srow.addWidget(self.btn_sess)
        close = _btn("Đóng", "ghost"); close.clicked.connect(self.reject)
        brow = QHBoxLayout(); brow.addWidget(self.btn_new); brow.addStretch(1); brow.addWidget(close)
        note = QLabel("Cookie đăng nhập chỉ lưu trên máy này (Windows mã hoá bằng DPAPI), không gửi đi đâu ngoài Bilibili.")
        note.setObjectName("hint"); note.setWordWrap(True)
        for w in (intro, self.qr, self.status):
            v.addWidget(w)
        v.addLayout(srow); v.addWidget(note); v.addLayout(brow)
        self.timer = QTimer(self); self.timer.setInterval(2000); self.timer.timeout.connect(self._poll)
        self._start()

    def _start(self):
        self.timer.stop()
        self.status.setText("đang lấy mã QR…")
        self.btn_new.setEnabled(False)

        def work():
            url, key = self.bili.qr_start()
            return qr_png(url), key
        j = _Run(work, self); j.result.connect(self._on_qr); _keep(self, j)

    def _on_qr(self, res, err):
        self.btn_new.setEnabled(True)
        if err:
            if "segno" in err:
                err = "Thiếu thư viện tạo mã QR (segno): chạy lại run.bat hoặc  pip install segno. Vẫn có thể dán SESSDATA."
            self.status.setText(f"✖ {err}")
            return
        png, self._key = res
        pm = QPixmap(); pm.loadFromData(png)
        self.qr.setPixmap(pm.scaled(260, 260, Qt.KeepAspectRatio, Qt.FastTransformation))
        self.status.setText("Chờ quét mã… (mã hết hạn sau khoảng 3 phút)")
        self.timer.start()

    def _poll(self):
        if self._polling or not self._key:
            return
        self._polling = True
        key = self._key
        j = _Run(lambda: self.bili.qr_poll(key), self); j.result.connect(self._on_poll); _keep(self, j)

    def _on_poll(self, res, err):
        self._polling = False
        if res == "ok":
            self.timer.stop()
            self.status.setText("✔ Đã đăng nhập")
            self.accept()
        elif res == "scanned":
            self.status.setText("Đã quét. Bấm <b>xác nhận đăng nhập</b> trên điện thoại…")
        elif res == "expired":
            self.timer.stop()
            self.status.setText("Mã đã hết hạn. Bấm “↻ Lấy mã mới”.")

    def _use_sess(self):
        val = self.ed_sess.text().strip()
        if val.lower().startswith("sessdata="):
            val = val.split("=", 1)[1]
        val = val.split(";")[0].strip()
        if not val:
            return
        self.btn_sess.setEnabled(False)
        self.status.setText("đang kiểm tra…")

        def work():
            self.bili.set_login({"SESSDATA": val})
            acc = self.bili.account()
            if not acc["login"]:
                self.bili.logout()
                raise BiliError("SESSDATA không đúng hoặc đã hết hạn")
            return acc
        j = _Run(work, self); j.result.connect(self._on_sess); _keep(self, j)

    def _on_sess(self, res, err):
        self.btn_sess.setEnabled(True)
        if err:
            self.status.setText(f"✖ {err}")
            return
        self.timer.stop()
        self.accept()

    def done(self, r):
        self.timer.stop()
        _release(self)
        super().done(r)


# ====================================================================== lấy phụ đề
FORMATS = {"SRT (.srt)": "srt", "WebVTT (.vtt)": "vtt", "Chữ thường (.txt)": "txt", "JSON gốc của Bilibili (.json)": "json"}


class SubtitleDialog(QDialog):
    """Lấy phụ đề có sẵn của một video (chọn phần/tập, chọn ngôn ngữ), xem trước, chép, lưu."""
    login_changed = Signal()

    def __init__(self, bili: Bili, title: str, parts: list[Part], out_dir: str, parent=None):
        super().__init__(parent)
        self.bili, self.title, self.parts, self.out_dir = bili, title, parts, out_dir
        self._jobs, self._tracks, self._body, self._cid = [], [], None, None
        self._cache: dict[str, list] = {}
        self.setWindowTitle(f"Phụ đề · {title}")
        self.resize(720, 600)
        v = QVBoxLayout(self); v.setSpacing(10)
        self.cb_part = QComboBox()
        for p in parts:
            self.cb_part.addItem(p.title, p)
        self.cb_part.setVisible(len(parts) > 1)
        self.cb_track = QComboBox(); self.cb_track.setMinimumWidth(320)
        self.status = QLabel(""); self.status.setObjectName("hint"); self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_login = _btn("🔑 Đăng nhập Bilibili", "primary", "Bilibili chỉ đưa phụ đề cho tài khoản đã đăng nhập")
        self.btn_login.clicked.connect(self._login)
        self.btn_login.setVisible(not bili.logged_in())
        top = QHBoxLayout(); top.setSpacing(8)
        lb = QLabel("Phụ đề:"); top.addWidget(lb); top.addWidget(self.cb_track, 1); top.addWidget(self.btn_login)
        self.view = QPlainTextEdit(); self.view.setReadOnly(True)
        self.view.setPlaceholderText("Nội dung phụ đề hiện ở đây")
        self.cb_fmt = QComboBox(); self.cb_fmt.addItems(FORMATS)
        self.btn_copy = _btn("📋 Chép chữ", "ghost", "Chép toàn bộ lời thoại (không có thời gian)")
        self.btn_save = _btn("💾 Lưu…", "primary")
        close = _btn("Đóng", "ghost"); close.clicked.connect(self.reject)
        brow = QHBoxLayout(); brow.setSpacing(8)
        brow.addWidget(QLabel("Định dạng:")); brow.addWidget(self.cb_fmt); brow.addWidget(self.btn_save)
        brow.addWidget(self.btn_copy); brow.addStretch(1); brow.addWidget(close)
        hint = QLabel("Chỉ lấy được phụ đề video đã có sẵn: do người đăng làm hoặc Bilibili tự tạo bằng AI (có ghi “AI”). "
                      "Chữ in cứng trong hình thì không lấy được; dùng tab Dịch phụ đề để nghe giọng tạo phụ đề. "
                      "Lưu dạng SRT với tên mặc định thì tab Dịch phụ đề dùng luôn file này.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        v.addWidget(self.cb_part); v.addLayout(top); v.addWidget(self.status); v.addWidget(self.view, 1)
        v.addWidget(hint); v.addLayout(brow)
        self.cb_part.currentIndexChanged.connect(lambda _=0: self._load_tracks())
        self.cb_track.currentIndexChanged.connect(lambda _=0: self._load_body())
        self.btn_copy.clicked.connect(self._copy)
        self.btn_save.clicked.connect(self._save)
        self._set_ready(False)
        self._load_tracks()

    # ------------------------------------------------------------
    def _set_ready(self, ok: bool):
        for w in (self.btn_copy, self.btn_save, self.cb_fmt):
            w.setEnabled(ok)

    def _part(self) -> Part:
        return self.cb_part.currentData()

    def _load_tracks(self):
        part = self._part()
        if part is None:
            return
        self._set_ready(False)
        self.cb_track.blockSignals(True); self.cb_track.clear(); self.cb_track.blockSignals(False)
        self.view.clear()
        self.status.setText("đang tìm phụ đề…")

        def work(url=part.url):
            bvid, cid, p, n = self.bili.cid_for(url)
            return url, (bvid, p, n), self.bili.subtitles(bvid, cid)
        j = _Run(work, self); j.result.connect(self._on_tracks); _keep(self, j)

    def _on_tracks(self, res, err):
        if err:
            self.status.setText(f"✖ {err}")
            self.btn_login.setVisible(not self.bili.logged_in())
            return
        url, self._cid, tracks = res
        if self._part() is None or url != self._part().url:
            return      # người dùng đã chọn phần khác
        self._tracks = tracks
        if not tracks:
            self.status.setText("Video này không có phụ đề có sẵn. Dùng tab Dịch phụ đề để nghe giọng và tạo phụ đề từ video.")
            return
        self.cb_track.blockSignals(True)
        for t in tracks:
            self.cb_track.addItem(t.label, t)
        # ưu tiên tiếng Trung do người đăng làm
        best = min(range(len(tracks)), key=lambda i: (tracks[i].code != "zh", tracks[i].ai))
        self.cb_track.setCurrentIndex(best)
        self.cb_track.blockSignals(False)
        self._load_body()

    def _load_body(self):
        t: SubTrack | None = self.cb_track.currentData()
        if t is None:
            return
        self._set_ready(False)
        if t.url in self._cache:
            self._show(t, self._cache[t.url])
            return
        self.status.setText("đang tải phụ đề…")
        j = _Run(lambda t=t: (t, self.bili.subtitle_body(t)), self); j.result.connect(self._on_body); _keep(self, j)

    def _on_body(self, res, err):
        if err:
            self.status.setText(f"✖ {err}")
            return
        t, body = res
        self._cache[t.url] = body
        if self.cb_track.currentData() is t:
            self._show(t, body)

    def _show(self, t: SubTrack, body: list):
        self._body = body
        end = body[-1].get("to", 0) if body else 0
        self.status.setText(f"{len(body)} câu · dài {_ts(end)[:8]} · {t.label}")
        self.view.setPlainText("\n".join(f"[{_ts(c.get('from', 0))[:8]}]  {(c.get('content') or '').strip()}" for c in body))
        self._set_ready(bool(body))

    # ------------------------------------------------------------
    def _text(self, fmt: str) -> str:
        if fmt == "srt":
            return subs_to_srt(self._body)
        if fmt == "vtt":
            return subs_to_vtt(self._body)
        if fmt == "txt":
            return subs_to_txt(self._body)
        return json.dumps({"body": self._body}, ensure_ascii=False, indent=2)

    def default_path(self, fmt: str) -> str:
        t: SubTrack = self.cb_track.currentData()
        bvid, p, n = self._cid or ("", 1, 1)
        # cùng tên với video tải về (“tên [BV…].mp4”) -> tab Dịch phụ đề tự nhận “tên [BV…].zh.srt”
        stem = f"{_safe_name(self._part().title if n > 1 else self.title) or bvid} [{bvid}" + (f"_p{p}" if n > 1 else "") + "]"
        return os.path.join(self.out_dir, f"{stem}.{t.code}.{fmt}")

    def _save(self):
        if not self._body:
            return
        fmt = FORMATS[self.cb_fmt.currentText()]
        os.makedirs(self.out_dir, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Lưu phụ đề", self.default_path(fmt), f"*.{fmt}")
        if not path:
            return
        self.save_to(path, fmt)

    def save_to(self, path: str, fmt: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._text(fmt))
        self.status.setText(f"✔ Đã lưu: {path}")

    def _copy(self):
        if self._body:
            QGuiApplication.clipboard().setText(subs_to_txt(self._body))
            self.status.setText("✔ Đã chép lời thoại")

    def _login(self):
        if LoginDialog(self.bili, self).exec() == QDialog.Accepted:
            self.btn_login.hide()
            self.login_changed.emit()
            self._load_tracks()

    def done(self, r):
        _release(self)
        super().done(r)
