"""Giao diện Subtitle Translator (PySide6)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import srt
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
def _box() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.StyledPanel)
    return f


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subtitle Translator")
        self.resize(1000, 620)
        PROMPTS.mkdir(parents=True, exist_ok=True)
        self.cfg = self._load_cfg()
        self.worker: Worker | None = None
        self.files: list[str] = []
        self.t0 = 0.0

        # ----- cột trái
        left = _box()
        lf = QFormLayout(left)
        lf.setLabelAlignment(Qt.AlignLeft)
        self.cb_provider = QComboBox()
        self.cb_provider.addItems(PROVIDERS.keys())
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)
        self.btn_models = QPushButton("Lấy model")
        mrow = QHBoxLayout()
        mrow.addWidget(self.cb_model, 1)
        mrow.addWidget(self.btn_models)
        self.ed_base = QLineEdit()
        self.ed_base.setPlaceholderText("vd: https://api.groq.com/openai/v1")
        self.ed_input = QLineEdit()
        self.ed_input.setReadOnly(True)
        self.ed_input.setPlaceholderText("Bấm để chọn 1 hoặc nhiều file .srt")
        self.btn_input = QPushButton("Chọn file…")
        irow = QHBoxLayout()
        irow.addWidget(self.ed_input, 1)
        irow.addWidget(self.btn_input)
        self.ed_output = QLineEdit()
        self.ed_output.setPlaceholderText("Để trống = cùng thư mục file gốc")
        self.btn_output = QPushButton("Chọn…")
        orow = QHBoxLayout()
        orow.addWidget(self.ed_output, 1)
        orow.addWidget(self.btn_output)
        self.sp_threads = QSpinBox(); self.sp_threads.setRange(1, 64)
        self.sp_rpm = QSpinBox(); self.sp_rpm.setRange(0, 10000)
        self.sp_rpm.setSpecialValueText("Không giới hạn")
        self.sp_batch = QSpinBox(); self.sp_batch.setRange(1, 300)

        def hint(widget, text):
            row = QHBoxLayout()
            row.addWidget(widget)
            lab = QLabel(text)
            lab.setStyleSheet("color:#c0392b; font-style:italic")
            row.addWidget(lab, 1)
            return row

        lf.addRow("Nhà cung cấp", self.cb_provider)
        lf.addRow("Mô hình", mrow)
        lf.addRow("Base URL", self.ed_base)
        lf.addRow("Input File", irow)
        lf.addRow("Output", orow)
        lf.addRow("Số luồng", hint(self.sp_threads, "Số batch gửi cùng lúc"))
        lf.addRow("Request/phút", hint(self.sp_rpm, "Đặt theo giới hạn (RPM) của gói API"))
        lf.addRow("Batch size", hint(self.sp_batch, "Số câu mỗi lần gửi (30–60 là vừa)"))

        # ----- cột phải
        right = _box()
        rg = QGridLayout(right)
        self.ed_keys = QPlainTextEdit()
        self.ed_keys.setPlaceholderText("Dán API key — nhiều key thì mỗi dòng một key, app sẽ xoay vòng")
        self.ed_keys.setFixedHeight(70)
        self.cb_target = QComboBox(); self.cb_target.addItems(LANGS)
        self.cb_source = QComboBox(); self.cb_source.addItems(LANGS)
        self.cb_prompt = QComboBox()
        self.btn_upload_prompt = QPushButton("Upload File Prompt")
        self.btn_edit_prompt = QPushButton("Sửa prompt")
        self.btn_del_prompt = QPushButton("Xóa file")
        self.btn_test = QPushButton("Thử kết nối")
        rg.addWidget(QLabel("API Key"), 0, 0, Qt.AlignTop)
        rg.addWidget(self.ed_keys, 0, 1, 1, 3)
        rg.addWidget(QLabel("Dịch ra"), 1, 0); rg.addWidget(self.cb_target, 1, 1)
        rg.addWidget(QLabel("Dịch từ"), 2, 0); rg.addWidget(self.cb_source, 2, 1)
        rg.addWidget(QLabel("Prompt"), 3, 0); rg.addWidget(self.cb_prompt, 3, 1, 1, 3)
        rg.addWidget(self.btn_upload_prompt, 4, 1, 1, 3)
        prow = QHBoxLayout()
        prow.addWidget(self.btn_edit_prompt); prow.addWidget(self.btn_del_prompt)
        prow.addWidget(self.btn_test); prow.addStretch(1)
        rg.addLayout(prow, 5, 1, 1, 3)
        rg.setColumnStretch(2, 1)
        rg.setRowStretch(6, 1)

        top = QHBoxLayout()
        top.addWidget(left, 1)
        top.addWidget(right, 1)

        # ----- tiến độ + nút
        self.bar = QProgressBar(); self.bar.setValue(0)
        self.bar.setStyleSheet("QProgressBar::chunk{background:#1e8a1e}")
        self.lb_batch = QLabel("Batch: 0/0")
        self.lb_file = QLabel("File: 0/0")
        self.lb_time = QLabel("Elapsed: 0s")
        prog = QHBoxLayout()
        prog.addWidget(QLabel("Progress"))
        prog.addWidget(self.bar, 1)
        for w in (self.lb_file, self.lb_batch, self.lb_time):
            w.setMinimumWidth(90)
            prog.addWidget(w)

        self.btn_run = QPushButton("Dịch")
        self.btn_stop = QPushButton("Dừng")
        self.btn_quit = QPushButton("Thoát")
        self.btn_fix = QPushButton("Check dịch sót")
        self.btn_fmt = QPushButton("Check format")
        btns = QHBoxLayout()
        for b in (self.btn_run, self.btn_stop, self.btn_quit):
            b.setMinimumWidth(100); btns.addWidget(b)
        btns.addStretch(1)
        for b in (self.btn_fix, self.btn_fmt):
            b.setMinimumWidth(120); btns.addWidget(b)
        self.btn_stop.setEnabled(False)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(5000)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addLayout(prog)
        v.addLayout(btns)
        v.addWidget(self.log_box, 1)
        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        # ----- sự kiện
        self.cb_provider.currentTextChanged.connect(self._on_provider)
        self.btn_models.clicked.connect(self._fetch_models)
        self.btn_input.clicked.connect(self._pick_input)
        self.ed_input.mousePressEvent = lambda e: self._pick_input()
        self.btn_output.clicked.connect(self._pick_output)
        self.btn_upload_prompt.clicked.connect(self._upload_prompt)
        self.btn_edit_prompt.clicked.connect(self._edit_prompt)
        self.btn_del_prompt.clicked.connect(self._delete_prompt)
        self.btn_test.clicked.connect(self._test_conn)
        self.btn_run.clicked.connect(lambda: self._start("translate"))
        self.btn_fix.clicked.connect(lambda: self._start("fix"))
        self.btn_stop.clicked.connect(self._stop)
        self.btn_quit.clicked.connect(self.close)
        self.btn_fmt.clicked.connect(self._check_format)

        self._apply_cfg()

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
        self.ed_base.setEnabled(custom)
        self.ed_base.setText(pc.get("base_url", "") if custom else p["base_url"])

    # ---------- chọn file
    def _pick_input(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn file phụ đề", self.cfg.get("last_dir", ""), "Phụ đề (*.srt);;Tất cả (*.*)")
        if files:
            self.files = files
            self.cfg["last_dir"] = str(Path(files[0]).parent)
            self.ed_input.setText(files[0] if len(files) == 1 else f"{len(files)} file: {Path(files[0]).name}, …")

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
        self.worker.file_progress.connect(lambda i, n: self.lb_file.setText(f"File: {i}/{n}"))
        self.worker.done.connect(self._on_done)
        self._set_running(True)
        self.bar.setValue(0)
        self.t0 = time.monotonic()
        self.timer.start(500)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self._log("Đang dừng… (chờ các request đang chạy kết thúc)")

    def _on_progress(self, done, total):
        self.lb_batch.setText(f"Batch: {done}/{total}")
        self.bar.setValue(int(done * 100 / total) if total else 0)

    def _on_done(self, msg):
        self.timer.stop()
        self._tick()
        self._set_running(False)
        if msg.startswith("Xong"):
            self.bar.setValue(100)
        self._log(f"■ {msg}")

    def _set_running(self, running: bool):
        for w in (self.btn_run, self.btn_fix, self.btn_fmt, self.cb_provider, self.btn_input):
            w.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def _tick(self):
        s = int(time.monotonic() - self.t0)
        self.lb_time.setText(f"Elapsed: {s // 60}m{s % 60:02d}s" if s >= 60 else f"Elapsed: {s}s")

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
        self.log_box.appendPlainText(msg)

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        try:
            self._save_cfg()
        except OSError:
            pass
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
