"""Giao diện Subtitle Translator (PySide6)."""
from __future__ import annotations

import html
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import srt, theme
from .providers import PROVIDERS, ProviderError, make_provider
from .translator import Settings, Stopped, Translator, needs_retranslation

LANGS = ["Tiếng Trung", "Tiếng Việt", "Tiếng Anh", "Tiếng Nhật", "Tiếng Hàn", "Tiếng Thái",
         "Tiếng Indonesia", "Tiếng Pháp", "Tiếng Tây Ban Nha", "Tiếng Nga"]
LANG_CODE = {"Tiếng Việt": "vi", "Tiếng Anh": "en", "Tiếng Trung": "zh", "Tiếng Nhật": "ja",
             "Tiếng Hàn": "ko", "Tiếng Thái": "th", "Tiếng Indonesia": "id", "Tiếng Pháp": "fr",
             "Tiếng Tây Ban Nha": "es", "Tiếng Nga": "ru"}
NO_PROMPT = "(Không dùng prompt riêng)"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DATA = app_dir() / "data"
PROMPTS = DATA / "prompts"
CONFIG = DATA / "settings.json"


def output_path_for(inp: str, out_dir: str, target_lang: str) -> str:
    p = Path(inp)
    folder = Path(out_dir) if out_dir else p.parent
    return str(folder / f"{p.stem}.{LANG_CODE.get(target_lang, 'out')}.srt")


# ---------------------------------------------------------------- worker
class Worker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    file_progress = Signal(int, int)
    done = Signal(str)

    def __init__(self, mode, files, out_dir, providers, settings):
        super().__init__()
        self.mode, self.files, self.out_dir = mode, files, out_dir
        self.providers, self.settings = providers, settings
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        tr = Translator(self.providers, self.settings, self.stop_event)
        tr.cb.log = self.log.emit
        tr.cb.progress = self.progress.emit
        ok = 0
        try:
            for fi, path in enumerate(self.files, 1):
                if self.stop_event.is_set():
                    break
                self.file_progress.emit(fi, len(self.files))
                self.log.emit(f"━━ [{fi}/{len(self.files)}] {Path(path).name}")
                cues = srt.parse(srt.read_text(path))
                if not cues:
                    self.log.emit("  ✖ Không đọc được phụ đề nào trong file")
                    continue
                out = output_path_for(path, self.out_dir, self.settings.target_lang)
                if self.mode == "translate":
                    res = tr.translate_cues(cues)
                else:  # fix: chỉ dịch lại câu sót trong file output đã có
                    if not os.path.exists(out):
                        self.log.emit(f"  ✖ Chưa có file kết quả: {out}")
                        continue
                    old = {i + 1: c.text for i, c in enumerate(srt.parse(srt.read_text(out)))}
                    if len(old) != len(cues):
                        self.log.emit(f"  ⚠ Số câu khác nhau (gốc {len(cues)}, đã dịch {len(old)}) — so theo thứ tự")
                    miss = [c.index for c in cues
                            if needs_retranslation(c.text, old.get(c.index), self.settings.source_lang)]
                    self.log.emit(f"  Tìm thấy {len(miss)} câu dịch sót")
                    if not miss:
                        ok += 1
                        continue
                    res = tr.translate_cues(cues, only_ids=miss, existing=old)
                left = [c.index for c in cues
                        if needs_retranslation(c.text, res.get(c.index), self.settings.source_lang)]
                new_cues = [srt.Cue(c.index, c.start, c.end, res.get(c.index) or c.text, c.extra)
                            for c in cues]
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                with open(out, "w", encoding="utf-8-sig", newline="\r\n") as f:
                    f.write(srt.compose(new_cues))
                msg = f"  ✔ Đã lưu {out}"
                if left:
                    preview = ", ".join(map(str, left[:15])) + ("…" if len(left) > 15 else "")
                    msg += f"\n  ⚠ Còn {len(left)} câu chưa dịch được (giữ nguyên bản gốc): {preview}"
                self.log.emit(msg)
                ok += 1
        except Stopped:
            self.done.emit("Đã dừng.")
            return
        except ProviderError as e:
            self.done.emit(f"Lỗi API (không thử lại được): {e}")
            return
        except Exception as e:  # noqa: BLE001
            self.done.emit(f"Lỗi: {type(e).__name__}: {e}")
            return
        self.done.emit("Đã dừng." if self.stop_event.is_set() else f"Xong {ok}/{len(self.files)} file.")


class Task(QThread):
    """Chạy một hàm ở luồng nền rồi trả kết quả (để giao diện không bị đơ)."""
    result = Signal(object, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit(None, str(e))


# ---------------------------------------------------------------- window
# ---------------------------------------------------------------- widget nhỏ
def card(title: str, step: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    v = QVBoxLayout(f)
    v.setContentsMargins(18, 16, 18, 18)
    v.setSpacing(12)
    head = QHBoxLayout()
    head.setSpacing(10)
    if step:
        s = QLabel(step); s.setObjectName("step"); head.addWidget(s)
    t = QLabel(title); t.setObjectName("cardTitle"); head.addWidget(t)
    head.addStretch(1)
    v.addLayout(head)
    return f, v


def field(label: str, widget, hint: str | None = None) -> QVBoxLayout:
    v = QVBoxLayout()
    v.setSpacing(5)
    lab = QLabel(label.upper()); lab.setObjectName("fieldLabel")
    v.addWidget(lab)
    if isinstance(widget, QWidget):
        v.addWidget(widget)
    else:
        v.addLayout(widget)
    if hint:
        h = QLabel(hint); h.setObjectName("hint"); h.setWordWrap(True)
        v.addWidget(h)
    return v


def btn(text: str, kind: str | None = None, tip: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def repolish(w: QWidget):
    w.style().unpolish(w)
    w.style().polish(w)


class DropZone(QFrame):
    """Ô kéo-thả file .srt (hoặc bấm để chọn)."""
    files_dropped = Signal(list)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(92)
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 12, 18, 12)
        h.setSpacing(14)
        icon = QLabel("⇪"); icon.setObjectName("dropIcon")
        h.addWidget(icon)
        col = QVBoxLayout(); col.setSpacing(2)
        self.title = QLabel("Kéo thả file .srt vào đây"); self.title.setObjectName("dropTitle")
        self.sub = QLabel("hoặc bấm để chọn — chọn nhiều file để dịch cả bộ phim")
        self.sub.setObjectName("muted")
        self.title.setWordWrap(True); self.sub.setWordWrap(True)
        col.addWidget(self.title); col.addWidget(self.sub)
        h.addLayout(col, 1)

    def set_files(self, files: list[str]):
        if not files:
            return
        if len(files) == 1:
            self.title.setText(Path(files[0]).name)
            self.sub.setText(str(Path(files[0]).parent))
        else:
            self.title.setText(f"{len(files)} file phụ đề")
            names = ", ".join(Path(f).name for f in files[:3])
            self.sub.setText(names + (" …" if len(files) > 3 else ""))

    def mousePressEvent(self, e):
        self.clicked.emit()

    def _set_active(self, on: bool):
        self.setProperty("active", on)
        repolish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction(); self._set_active(True)

    def dragLeaveEvent(self, e):
        self._set_active(False)

    def dropEvent(self, e):
        self._set_active(False)
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile().lower().endswith(".srt")]
        if files:
            self.files_dropped.emit(files)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subtitle Translator")
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 820)
        self.setMinimumSize(1000, 800)
        PROMPTS.mkdir(parents=True, exist_ok=True)
        self.cfg = self._load_cfg()
        self.worker: Worker | None = None
        self.files: list[str] = []
        self.t0 = 0.0

        # ===== header
        title = QLabel("Subtitle Translator"); title.setObjectName("appTitle")
        sub = QLabel("Dịch phụ đề .srt bằng AI — Gemini · Claude · OpenAI · DeepSeek · OpenRouter")
        sub.setObjectName("appSub")
        tcol = QVBoxLayout(); tcol.setSpacing(2)
        tcol.addWidget(title); tcol.addWidget(sub)
        self.pill = QLabel("●  Sẵn sàng"); self.pill.setObjectName("pill")
        header = QHBoxLayout()
        logo = QLabel(); logo.setPixmap(theme.app_icon().pixmap(44, 44))
        header.addWidget(logo); header.addSpacing(6)
        header.addLayout(tcol, 1)
        header.addWidget(self.pill, 0, Qt.AlignTop)

        # ===== thẻ 1: Kết nối AI
        c1, v1 = card("Kết nối AI", "1")
        self.cb_provider = QComboBox(); self.cb_provider.addItems(PROVIDERS.keys())
        self.cb_model = QComboBox(); self.cb_model.setEditable(True)
        self.cb_model.lineEdit().setPlaceholderText("Bấm “Lấy model” hoặc gõ tên model")
        self.btn_models = btn("Lấy model", "ghost", "Tải danh sách model thật của API key")
        mrow = QHBoxLayout(); mrow.setSpacing(8)
        mrow.addWidget(self.cb_model, 1); mrow.addWidget(self.btn_models)
        self.ed_base = QLineEdit(); self.ed_base.setPlaceholderText("vd: https://api.groq.com/openai/v1")
        self.ed_keys = QPlainTextEdit()
        self.ed_keys.setPlaceholderText("Dán API key…\nNhiều key: mỗi dòng một key, app sẽ xoay vòng")
        self.ed_keys.setMinimumHeight(64); self.ed_keys.setMaximumHeight(90)
        self.btn_test = btn("⚡  Thử kết nối", "ghost")
        self.base_wrap = QWidget()
        bw = QVBoxLayout(self.base_wrap); bw.setContentsMargins(0, 0, 0, 0)
        bw.addLayout(field("Base URL", self.ed_base))
        v1.addLayout(field("Nhà cung cấp", self.cb_provider))
        v1.addLayout(field("Mô hình", mrow))
        v1.addWidget(self.base_wrap)
        v1.addLayout(field("API key", self.ed_keys))
        trow = QHBoxLayout(); trow.addWidget(self.btn_test); trow.addStretch(1)
        v1.addLayout(trow)
        v1.addStretch(1)

        # ===== thẻ 2: Phụ đề
        c2, v2 = card("Phụ đề", "2")
        self.drop = DropZone()
        self.ed_output = QLineEdit(); self.ed_output.setPlaceholderText("Để trống = lưu cạnh file gốc")
        self.btn_output = btn("Chọn…", "ghost")
        orow = QHBoxLayout(); orow.setSpacing(8)
        orow.addWidget(self.ed_output, 1); orow.addWidget(self.btn_output)
        self.cb_source = QComboBox(); self.cb_source.addItems(LANGS)
        self.cb_target = QComboBox(); self.cb_target.addItems(LANGS)
        self.btn_swap = btn("⇄", "ghost", "Đổi chiều dịch"); self.btn_swap.setFixedWidth(44)
        lrow = QHBoxLayout(); lrow.setSpacing(8)
        lrow.addLayout(field("Dịch từ", self.cb_source), 1)
        swap_col = QVBoxLayout(); swap_col.addStretch(1); swap_col.addWidget(self.btn_swap)
        lrow.addLayout(swap_col)
        lrow.addLayout(field("Dịch ra", self.cb_target), 1)
        self.cb_prompt = QComboBox()
        self.btn_upload_prompt = btn("Tải lên", "link", "Thêm file prompt .txt")
        self.btn_edit_prompt = btn("Sửa", "link", "Mở file prompt để sửa (chưa có thì tạo mẫu)")
        self.btn_del_prompt = btn("Xóa", "link", "Xóa file prompt đang chọn")
        prow = QHBoxLayout(); prow.setSpacing(2)
        prow.addWidget(self.cb_prompt, 1)
        for b in (self.btn_upload_prompt, self.btn_edit_prompt, self.btn_del_prompt):
            prow.addWidget(b)
        v2.addWidget(self.drop)
        v2.addLayout(field("Lưu vào thư mục", orow))
        v2.addLayout(lrow)
        v2.addLayout(field("Prompt riêng cho phim", prow,
                           "Tên nhân vật, xưng hô, giữ từ Hán Việt… — dòng bắt đầu bằng # là ghi chú"))

        # ===== thẻ 3: Tốc độ
        c3, v3 = card("Tốc độ", "3")
        self.sp_threads = QSpinBox(); self.sp_threads.setRange(1, 64)
        self.sp_rpm = QSpinBox(); self.sp_rpm.setRange(0, 10000)
        self.sp_rpm.setSpecialValueText("Không giới hạn")
        self.sp_batch = QSpinBox(); self.sp_batch.setRange(1, 300)
        srow = QHBoxLayout(); srow.setSpacing(14); srow.setAlignment(Qt.AlignTop)
        srow.addLayout(field("Số luồng", self.sp_threads, "Số batch gửi cùng lúc"), 1)
        srow.addLayout(field("Request / phút", self.sp_rpm, "Theo RPM của gói API"), 1)
        srow.addLayout(field("Batch size", self.sp_batch, "Câu mỗi lần gửi (30–60)"), 1)
        v3.addLayout(srow)

        # ===== thẻ nhật ký
        c4, v4 = card("Nhật ký")
        self.btn_clear = btn("Xóa", "link")
        v4.itemAt(0).layout().addWidget(self.btn_clear)
        self.log_box = QPlainTextEdit(); self.log_box.setObjectName("log")
        self.log_box.setReadOnly(True); self.log_box.setMaximumBlockCount(5000)
        self.log_box.setMinimumHeight(120)
        self.log_box.setPlaceholderText("Nhật ký sẽ hiện ở đây…")
        v4.addWidget(self.log_box, 1)

        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(c1, 1); left.addWidget(c3)
        right = QVBoxLayout(); right.setSpacing(14)
        right.addWidget(c2); right.addWidget(c4, 1)
        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addLayout(left, 1); cols.addLayout(right, 1)

        # ===== thanh hành động
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 16, 18, 16); av.setSpacing(12)
        self.bar = QProgressBar(); self.bar.setValue(0); self.bar.setFixedHeight(12)
        self.lb_pct = QLabel("0%"); self.lb_pct.setObjectName("cardTitle"); self.lb_pct.setMinimumWidth(48)
        self.lb_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        brow = QHBoxLayout(); brow.addWidget(self.bar, 1); brow.addWidget(self.lb_pct)
        self.lb_file = QLabel("File  0/0"); self.lb_batch = QLabel("Batch  0/0")
        self.lb_time = QLabel("⏱  0s")
        for w in (self.lb_file, self.lb_batch, self.lb_time):
            w.setObjectName("chip")
        self.btn_run = btn("▶   Bắt đầu dịch", "primary")
        self.btn_stop = btn("■  Dừng", "danger")
        self.btn_fix = btn("Check dịch sót", "ghost", "Tìm câu trống / còn chữ gốc trong file đã dịch và dịch lại")
        self.btn_fmt = btn("Check format", "ghost", "Kiểm tra số thứ tự, timecode, dòng trống")
        self.btn_stop.setEnabled(False)
        crow = QHBoxLayout(); crow.setSpacing(8)
        for w in (self.lb_file, self.lb_batch, self.lb_time):
            crow.addWidget(w)
        crow.addStretch(1)
        crow.addWidget(self.btn_fix); crow.addWidget(self.btn_fmt)
        crow.addSpacing(10)
        crow.addWidget(self.btn_stop); crow.addWidget(self.btn_run)
        av.addLayout(brow); av.addLayout(crow)


        root = QWidget(); root.setObjectName("root")
        v = QVBoxLayout(root); v.setContentsMargins(22, 18, 22, 18); v.setSpacing(14)
        v.addLayout(header)
        v.addLayout(cols, 1)
        v.addWidget(act)
        self.setCentralWidget(root)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        # ===== sự kiện
        self.cb_provider.currentTextChanged.connect(self._on_provider)
        self.btn_models.clicked.connect(self._fetch_models)
        self.drop.clicked.connect(self._pick_input)
        self.drop.files_dropped.connect(self._set_files)
        self.btn_output.clicked.connect(self._pick_output)
        self.btn_swap.clicked.connect(self._swap_langs)
        self.btn_upload_prompt.clicked.connect(self._upload_prompt)
        self.btn_edit_prompt.clicked.connect(self._edit_prompt)
        self.btn_del_prompt.clicked.connect(self._delete_prompt)
        self.btn_test.clicked.connect(self._test_conn)
        self.btn_run.clicked.connect(lambda: self._start("translate"))
        self.btn_fix.clicked.connect(lambda: self._start("fix"))
        self.btn_stop.clicked.connect(self._stop)
        self.btn_fmt.clicked.connect(self._check_format)
        self.btn_clear.clicked.connect(self.log_box.clear)

        self._apply_cfg()

    def _set_pill(self, text: str, state: str = ""):
        self.pill.setText(f"●  {text}")
        self.pill.setProperty("state", state)
        repolish(self.pill)

    def _swap_langs(self):
        s, t = self.cb_source.currentText(), self.cb_target.currentText()
        self.cb_source.setCurrentText(t); self.cb_target.setCurrentText(s)

    # ---------- cấu hình
    def _load_cfg(self) -> dict:
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _apply_cfg(self):
        c = self.cfg
        self._refresh_prompts(c.get("prompt"))
        self.cb_provider.setCurrentText(c.get("provider", "Gemini"))
        self._on_provider(self.cb_provider.currentText())
        self.ed_output.setText(c.get("output", ""))
        self.sp_threads.setValue(c.get("threads", 10))
        self.sp_rpm.setValue(c.get("rpm", 0))
        self.sp_batch.setValue(c.get("batch", 40))
        self.cb_target.setCurrentText(c.get("target", "Tiếng Việt"))
        self.cb_source.setCurrentText(c.get("source", "Tiếng Trung"))

    def _save_provider_state(self, name: str):
        pc = self.cfg.setdefault("per_provider", {}).setdefault(name, {})
        pc["keys"] = self.ed_keys.toPlainText().strip()
        pc["model"] = self.cb_model.currentText().strip()
        pc["base_url"] = self.ed_base.text().strip()

    def _save_cfg(self):
        self._save_provider_state(self.cb_provider.currentText())
        self.cfg.update(
            provider=self.cb_provider.currentText(), output=self.ed_output.text(),
            threads=self.sp_threads.value(), rpm=self.sp_rpm.value(), batch=self.sp_batch.value(),
            target=self.cb_target.currentText(), source=self.cb_source.currentText(),
            prompt=self.cb_prompt.currentText(),
        )
        DATA.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    _current_provider = None

    def _on_provider(self, name: str):
        if self._current_provider and self._current_provider != name:
            self._save_provider_state(self._current_provider)
        self._current_provider = name
        p = PROVIDERS[name]
        pc = self.cfg.get("per_provider", {}).get(name, {})
        self.ed_keys.setPlainText(pc.get("keys", ""))
        self.cb_model.clear()
        models = list(dict.fromkeys(pc.get("models_cache", []) or p["models"]))
        self.cb_model.addItems(models)
        self.cb_model.setCurrentText(pc.get("model") or (models[0] if models else ""))
        custom = not p["base_url"]
        self.base_wrap.setVisible(custom)
        self.ed_base.setText(pc.get("base_url", "") if custom else p["base_url"])

    # ---------- chọn file
    def _pick_input(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn file phụ đề", self.cfg.get("last_dir", ""), "Phụ đề (*.srt);;Tất cả (*.*)")
        self._set_files(files)

    def _set_files(self, files: list[str]):
        if files:
            self.files = files
            self.cfg["last_dir"] = str(Path(files[0]).parent)
            self.drop.set_files(files)

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu", self.ed_output.text())
        if d:
            self.ed_output.setText(d)

    # ---------- prompt
    def _refresh_prompts(self, select: str | None = None):
        self.cb_prompt.clear()
        self.cb_prompt.addItem(NO_PROMPT)
        self.cb_prompt.addItems(sorted(p.name for p in PROMPTS.glob("*.txt")))
        if select:
            self.cb_prompt.setCurrentText(select)

    def _upload_prompt(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn file prompt", "", "Văn bản (*.txt *.md);;Tất cả (*.*)")
        if not f:
            return
        dst = PROMPTS / (Path(f).stem + ".txt")
        shutil.copyfile(f, dst)
        self._refresh_prompts(dst.name)
        self._log(f"Đã thêm prompt: {dst.name}")

    def _edit_prompt(self):
        name = self.cb_prompt.currentText()
        if name == NO_PROMPT:
            name = "prompt-moi.txt"
            (PROMPTS / name).write_text(
                "# Ghi hướng dẫn riêng cho phim, ví dụ:\n"
                "# - Thể loại: tiên hiệp/tu tiên, giữ từ Hán Việt (sư tôn, đạo hữu, linh căn...)\n"
                "# - Tên nhân vật: 林动 = Lâm Động\n"
                "# - Xưng hô: sư phụ - đồ nhi\n", encoding="utf-8")
            self._refresh_prompts(name)
        path = PROMPTS / name
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "Prompt", f"Mở file để sửa:\n{path}")

    def _delete_prompt(self):
        name = self.cb_prompt.currentText()
        if name == NO_PROMPT:
            return
        if QMessageBox.question(self, "Xóa prompt", f"Xóa {name}?") == QMessageBox.Yes:
            (PROMPTS / name).unlink(missing_ok=True)
            self._refresh_prompts()

    def _prompt_text(self) -> str:
        name = self.cb_prompt.currentText()
        if name == NO_PROMPT:
            return ""
        try:
            text = srt.read_text(str(PROMPTS / name))
        except OSError:
            return ""
        return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))

    # ---------- provider
    def _keys(self) -> list[str]:
        return [k.strip() for k in self.ed_keys.toPlainText().splitlines() if k.strip()]

    def _make_providers(self, need_key=True):
        keys = self._keys()
        if need_key and not keys:
            raise ProviderError("Chưa nhập API key")
        model = self.cb_model.currentText().strip()
        if need_key and not model:
            raise ProviderError("Chưa chọn mô hình")
        return [make_provider(self.cb_provider.currentText(), k, model, self.ed_base.text())
                for k in (keys or [""])]

    def _fetch_models(self):
        try:
            prov = self._make_providers(need_key=False)[0]
        except ProviderError as e:
            return QMessageBox.warning(self, "Lỗi", str(e))
        self.btn_models.setEnabled(False)
        self._log("Đang lấy danh sách model…")
        self._fetcher = Task(prov.list_models)
        self._fetcher.result.connect(self._on_models)
        self._fetcher.start()

    def _on_models(self, models, err):
        self.btn_models.setEnabled(True)
        if err:
            self._log(f"✖ Không lấy được model: {err}")
            return
        cur = self.cb_model.currentText()
        self.cb_model.clear()
        self.cb_model.addItems(models)
        self.cb_model.setCurrentText(cur if cur in models else (models[0] if models else cur))
        self.cfg.setdefault("per_provider", {}).setdefault(
            self.cb_provider.currentText(), {})["models_cache"] = models
        self._log(f"✔ Có {len(models)} model")

    def _test_conn(self):
        try:
            prov = self._make_providers()[0]
        except ProviderError as e:
            return QMessageBox.warning(self, "Lỗi", str(e))
        self._log("Đang thử kết nối…")
        self.btn_test.setEnabled(False)
        self._tester = Task(lambda: prov.chat("Trả lời đúng một từ.", "Dịch sang tiếng Việt: 你好"))
        self._tester.result.connect(self._on_test)
        self._tester.start()

    def _on_test(self, r, err):
        self.btn_test.setEnabled(True)
        self._log(f"✖ Lỗi: {err}" if err else f"✔ Kết nối OK — model trả lời: {str(r).strip()[:80]}")

    # ---------- chạy
    def _start(self, mode: str):
        if not self.files:
            return QMessageBox.warning(self, "Thiếu file", "Chưa chọn file .srt")
        try:
            providers = self._make_providers()
        except ProviderError as e:
            return QMessageBox.warning(self, "Lỗi", str(e))
        self._save_cfg()
        settings = Settings(
            source_lang=self.cb_source.currentText(), target_lang=self.cb_target.currentText(),
            threads=self.sp_threads.value(), batch_size=self.sp_batch.value(),
            rpm=self.sp_rpm.value(), custom_prompt=self._prompt_text(),
        )
        self.worker = Worker(mode, list(self.files), self.ed_output.text().strip(), providers, settings)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._on_progress)
        self.worker.file_progress.connect(lambda i, n: self.lb_file.setText(f"File  {i}/{n}"))
        self.worker.done.connect(self._on_done)
        self._set_running(True)
        self._set_pct(0)
        self.t0 = time.monotonic()
        self.timer.start(500)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self._log("Đang dừng… (chờ các request đang chạy kết thúc)")

    def _on_progress(self, done, total):
        self.lb_batch.setText(f"Batch  {done}/{total}")
        self._set_pct(int(done * 100 / total) if total else 0)

    def _set_pct(self, v: int):
        self.bar.setValue(v)
        self.lb_pct.setText(f"{v}%")

    def _on_done(self, msg):
        self.timer.stop()
        self._tick()
        self._set_running(False)
        if msg.startswith("Xong"):
            self._set_pct(100)
            self._set_pill("Hoàn tất", "ok")
        elif msg.startswith("Đã dừng"):
            self._set_pill("Đã dừng")
        else:
            self._set_pill("Có lỗi", "err")
        self._log(f"■ {msg}")

    def _set_running(self, running: bool):
        for w in (self.btn_run, self.btn_fix, self.btn_fmt, self.cb_provider, self.drop):
            w.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if running:
            self._set_pill("Đang dịch…", "run")

    def _tick(self):
        s = int(time.monotonic() - self.t0)
        self.lb_time.setText(f"⏱  {s // 60}:{s % 60:02d}" if s >= 60 else f"⏱  {s}s")

    # ---------- check format
    def _check_format(self):
        if not self.files:
            return QMessageBox.warning(self, "Thiếu file", "Chưa chọn file .srt")
        target = self.cb_target.currentText()
        bad = []
        for f in self.files:
            out = output_path_for(f, self.ed_output.text().strip(), target)
            path = out if os.path.exists(out) else f
            errs = srt.check_format(srt.read_text(path))
            self._log(f"Check format {Path(path).name}: " + ("OK ✔" if not errs else f"{len(errs)} lỗi"))
            for e in errs[:20]:
                self._log(f"   - {e}")
            if errs:
                bad.append(path)
        if bad and QMessageBox.question(
                self, "Sửa format", f"{len(bad)} file có lỗi. Tự sửa (đánh lại số, chuẩn hoá timecode)?\n"
                "File gốc sẽ được sao lưu thành .bak") == QMessageBox.Yes:
            for path in bad:
                shutil.copyfile(path, path + ".bak")
                cues = srt.parse(srt.read_text(path))
                with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                    fh.write(srt.compose(cues))
                self._log(f"✔ Đã sửa {Path(path).name} ({len(cues)} câu)")

    def _log(self, msg: str):
        for line in msg.split("\n"):
            t = line.strip()
            color = (theme.ACCENT if t.startswith(("✔", "■ Xong")) else theme.WARN if t.startswith("⚠")
                     else theme.DANGER if t.startswith("✖") else theme.TEXT if t.startswith("━")
                     else theme.MUTED)
            weight = "600" if t.startswith(("━", "■")) else "400"
            safe = html.escape(line).replace("  ", "&nbsp;&nbsp;")
            self.log_box.appendHtml(f'<span style="color:{color}; font-weight:{weight}">{safe}</span>')

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        try:
            self._save_cfg()
        except OSError:
            pass
        e.accept()


def _dark_title_bar(w):
    """Thanh tiêu đề tối trên Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(w.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (mới / cũ)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(theme.app_font())
    app.setStyleSheet(theme.stylesheet())
    app.setWindowIcon(theme.app_icon())
    w = MainWindow()
    w.show()
    _dark_title_bar(w)
    sys.exit(app.exec())
