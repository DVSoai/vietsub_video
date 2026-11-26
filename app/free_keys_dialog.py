"""“Kết nối AI miễn phí”: lấy key một lần, app tự nhận, tự kiểm tra, tự bật — sau đó không phải nhập lại.

Key do chính nhà cung cấp cấp cho tài khoản của người dùng (app không tự tạo key được). Hộp thoại này rút gọn
việc đó còn: bấm “Mở trang lấy key” -> bấm Copy trên trang -> app tự thấy key trong clipboard, đoán đúng nhà
cung cấp theo tiền tố key, thử gọi 1 lần, lưu và bật lên. Model chạy trên máy (Hugging Face) thì không cần key.
"""
from __future__ import annotations

import re
import requests

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .providers import PROVIDERS, make_provider

# tiền tố key để tự nhận ra nhà cung cấp khi người dùng bấm Copy trên trang
KEY_PATTERNS = {
    "Groq (miễn phí)": r"^gsk_[A-Za-z0-9]{20,}$",
    "Cerebras (miễn phí)": r"^csk-[A-Za-z0-9]{20,}$",
    "NVIDIA NIM (miễn phí)": r"^nvapi-[A-Za-z0-9_\-]{20,}$",
    "Zhipu GLM (miễn phí)": r"^[0-9a-f]{32}\.[A-Za-z0-9]{10,}$",
    "SiliconFlow (miễn phí)": r"^sk-[a-z]{40,}$",
    "OpenRouter": r"^sk-or-v1-[0-9a-f]{40,}$",
    "Gemini": r"^AIza[0-9A-Za-z_\-]{30,}$",
}
EXTRA = {   # nhà cung cấp có gói miễn phí sẵn trong app nhưng không thuộc nhóm "(miễn phí)"
    "Gemini": ("https://aistudio.google.com/apikey", "Google AI Studio: Gemini Flash có hạn mức miễn phí mỗi ngày."),
    "OpenRouter": ("https://openrouter.ai/keys", "Các model có đuôi :free gọi miễn phí (giới hạn lượt/ngày)."),
}


def guess_provider(text: str) -> str:
    t = (text or "").strip()
    for name, pat in KEY_PATTERNS.items():
        if re.match(pat, t):
            return name
    return ""


class _Check(QThread):
    done = Signal(str, str, str)       # tên, kết quả, lỗi

    def __init__(self, name: str, key: str, model: str):
        super().__init__()
        self.name, self.key, self.model = name, key, model

    def run(self):
        try:
            if self.name == "Groq (miễn phí)":
                response = requests.get("https://api.groq.com/openai/v1/models",
                                        headers={"Authorization": "Bearer " + self.key},
                                        timeout=15, allow_redirects=False)
                if response.status_code != 200:
                    self.done.emit(self.name, "", f"Groq HTTP {response.status_code}")
                    return
                models = {m.get("id") for m in response.json().get("data", [])}
                if self.model not in models:
                    self.done.emit(self.name, "", "Model đã chọn không có trong danh sách Groq")
                    return
                self.done.emit(self.name, "API key và model hợp lệ", "")
                return
            p = make_provider(self.name, self.key, self.model)
            r = p.chat("Trả lời đúng một từ tiếng Việt.", "Dịch: 你好")
            self.done.emit(self.name, (r or "").strip()[:40] or "OK", "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(self.name, "", f"Không thể kiểm tra kết nối ({type(e).__name__})")


class FreeKeysDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kết nối AI miễn phí")
        self.cfg = cfg
        self.rows: dict[str, dict] = {}
        self._jobs: list[_Check] = []
        self.changed = False
        names = [n for n, p in PROVIDERS.items() if p.get("free")] + list(EXTRA)

        top = QLabel("Mỗi nhà chỉ cần làm <b>một lần</b>: bấm <b>Mở trang lấy key</b>, đăng nhập, tạo key rồi bấm "
                     "<b>Copy</b> trên trang. App tự nhận key trong clipboard, kiểm tra và bật lên. Lúc dịch, app dùng "
                     "tất cả nhà đã có key; nhà nào hết lượt thì tự chuyển sang nhà khác.")
        top.setWordWrap(True); top.setTextFormat(Qt.RichText)
        self.chk_watch = QCheckBox("Tự nhận key khi tôi bấm Copy (theo dõi clipboard lúc hộp thoại đang mở)")
        self.chk_watch.setChecked(False)

        grid_w = QWidget(); grid = QGridLayout(grid_w); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(8)
        for r, name in enumerate(names):
            p = PROVIDERS[name]
            url, note = (p.get("signup"), p.get("note", "")) if p.get("free") else EXTRA[name]
            title = QLabel(f"<b>{name.replace(' (miễn phí)', '')}</b><br><span style='color:#8a94a7'>{note}</span>")
            title.setTextFormat(Qt.RichText); title.setWordWrap(True); title.setMinimumWidth(260)
            ed = QLineEdit(); ed.setPlaceholderText("dán key… (hoặc bấm Copy trên trang)"); ed.setEchoMode(QLineEdit.Password)
            have = self._keys(name)
            if have:
                ed.setText(have[0])
            b_open = QPushButton("Mở trang lấy key"); b_open.setObjectName("ghost")
            b_check = QPushButton("Kiểm tra && bật"); b_check.setObjectName("ghost")
            st = QLabel(self._status_text(name)); st.setObjectName("hint"); st.setMinimumWidth(170); st.setWordWrap(True)
            b_open.clicked.connect(lambda _=False, u=url, n=name: self._open_provider(n, u))
            b_check.clicked.connect(lambda _=False, n=name: self._check(n))
            ed.returnPressed.connect(lambda n=name: self._check(n))
            grid.addWidget(title, r, 0); grid.addWidget(ed, r, 1); grid.addWidget(b_open, r, 2)
            grid.addWidget(b_check, r, 3); grid.addWidget(st, r, 4)
            self.rows[name] = {"ed": ed, "st": st, "btn": b_check}
        grid.setColumnStretch(1, 1)
        sc = QScrollArea(); sc.setWidgetResizable(True); sc.setWidget(grid_w); sc.setFrameShape(QFrame.NoFrame)
        sc.setObjectName("freeScroll"); grid_w.setObjectName("freeGrid")
        sc.setStyleSheet("QScrollArea#freeScroll, QWidget#freeGrid { background: transparent; }")

        local = QLabel("Không muốn dùng key? Chọn <b>Hugging Face trên máy (miễn phí)</b> ở ô Nhà cung cấp "
                       "(dịch bằng Qwen chạy trên card) và <b>HF Qwen3-ASR</b> ở ô nghe giọng — không cần tài khoản.")
        local.setWordWrap(True); local.setTextFormat(Qt.RichText); local.setObjectName("hint")
        btns = QDialogButtonBox(QDialogButtonBox.Close); btns.button(QDialogButtonBox.Close).setText("Xong")
        btns.rejected.connect(self.accept)
        v = QVBoxLayout(self); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(10)
        v.addWidget(top); v.addWidget(self.chk_watch); v.addWidget(sc, 1); v.addWidget(local); v.addWidget(btns)
        self.resize(1060, 620)

        self._clip_last = (QGuiApplication.clipboard().text() or "").strip()
        self._clip_timer = QTimer(self); self._clip_timer.timeout.connect(self._poll_clipboard)
        self._clip_timer.start(700)

    # ---- trạng thái lưu trong cfg["per_provider"]
    def _pc(self, name: str) -> dict:
        return self.cfg.setdefault("per_provider", {}).setdefault(name, {})

    def _keys(self, name: str) -> list[str]:
        return [k.strip() for k in self._pc(name).get("keys", "").splitlines() if k.strip()]

    def _status_text(self, name: str) -> str:
        pc = self._pc(name)
        if self._keys(name) and pc.get("enabled"):
            return "✔ đang dùng"
        if self._keys(name):
            return "đã có key (đang tắt)"
        return "chưa có key"

    def _open_provider(self, name, url):
        # Explicit user action; never automate login or read browser credentials.
        self.rows[name]["st"].setText("Tạo key trên trang chính thức, sau đó dán vào ô key.")
        if name != "Groq (miễn phí)":
            QDesktopServices.openUrl(QUrl(url))
            return
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            QDesktopServices.openUrl(QUrl(url))
            return
        browser = QDialog(self)
        browser.setWindowTitle("Groq Console")
        browser.resize(1050, 720)
        layout = QVBoxLayout(browser)
        hint = QLabel("Đăng nhập, tạo key và bấm Copy trên Groq; sau đó bấm Nhận key.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        view = QWebEngineView(browser)
        layout.addWidget(view, 1)
        actions = QHBoxLayout()
        external = QPushButton("Mở trình duyệt hệ thống")
        external.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        receive = QPushButton("Nhận key đã sao chép")
        def receive_key():
            key = QGuiApplication.clipboard().text().strip()
            if guess_provider(key) != name:
                hint.setText("Clipboard chưa có API key Groq hợp lệ.")
                return
            if not self.rows[name]["btn"].isEnabled():
                hint.setText("Đang kiểm tra key trước đó.")
                return
            self.rows[name]["ed"].setText(key)
            browser.accept()
            self._check(name)
        receive.clicked.connect(receive_key)
        actions.addWidget(external)
        actions.addWidget(receive)
        layout.addLayout(actions)
        view.setUrl(QUrl(url))
        browser.exec()
        browser.deleteLater()

    # ---- clipboard
    def _poll_clipboard(self):
        if not self.chk_watch.isChecked():
            return
        t = (QGuiApplication.clipboard().text() or "").strip()
        if not t or t == self._clip_last:
            return
        self._clip_last = t
        name = guess_provider(t)
        if name and name in self.rows and self.rows[name]["btn"].isEnabled() and t not in self._keys(name):
            self.rows[name]["ed"].setText(t)
            self.rows[name]["st"].setText("nhận key từ clipboard, đang kiểm tra…")
            self._check(name)

    # ---- kiểm tra + lưu + bật
    def _check(self, name: str):
        if not self.rows[name]["btn"].isEnabled():
            return
        key = self.rows[name]["ed"].text().strip()
        if not key:
            self.rows[name]["st"].setText("chưa có key")
            return
        model = self._pc(name).get("model") or (PROVIDERS[name]["models"] or [""])[0]
        if not model:
            model = "openai/gpt-oss-20b:free" if name == "OpenRouter" else ""
        self.rows[name]["btn"].setEnabled(False)
        self.rows[name]["ed"].setEnabled(False)
        self.rows[name]["pending_key"] = key
        self.rows[name]["pending_model"] = model
        self.rows[name]["st"].setText("đang thử gọi…")
        job = _Check(name, key, model)
        job.done.connect(self._checked)
        self._jobs.append(job)
        job.start()

    def _checked(self, name, result, err):
        row = self.rows[name]
        row["btn"].setEnabled(True)
        row["ed"].setEnabled(True)
        key = row.pop("pending_key", "")
        model = row.pop("pending_model", "")
        if err:
            row["st"].setText(f"✖ {err}")
            return
        if not key:
            return
        pc = self._pc(name)
        keys = self._keys(name)
        if key not in keys:
            keys.append(key)
        pc["keys"] = "\n".join(keys)
        pc["enabled"] = True
        pc["model"] = model
        self.changed = True
        row["st"].setText(f"✔ đang dùng — {result}")

    def done(self, r):
        self._clip_timer.stop()
        if any(j.isRunning() for j in self._jobs):
            self._clip_timer.start(700)
            return
        super().done(r)
