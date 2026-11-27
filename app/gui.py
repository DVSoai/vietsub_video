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

from PySide6.QtCore import QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QStackedWidget,
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QAbstractItemView, QHeaderView, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from . import srt, theme
from .secure_store import protect_config, restore_config, restore_config_partial
from .providers import PROVIDERS, ProviderError, make_provider, project_from_json
from .transcribe import MEDIA_EXT, Transcriber, is_media
from .translator import Settings, Stopped, Translator, load_glossary, needs_retranslation

VERTEX_LOCATIONS = ["global", "us-central1", "asia-southeast1", "asia-northeast1", "asia-east1",
                    "us-east4", "us-west1", "europe-west1", "europe-west4"]
LANGS = ["Tiếng Trung", "Tiếng Việt", "Tiếng Anh", "Tiếng Nhật", "Tiếng Hàn", "Tiếng Thái",
         "Tiếng Indonesia", "Tiếng Pháp", "Tiếng Tây Ban Nha", "Tiếng Nga"]
LANG_CODE = {"Tiếng Việt": "vi", "Tiếng Anh": "en", "Tiếng Trung": "zh", "Tiếng Nhật": "ja",
             "Tiếng Hàn": "ko", "Tiếng Thái": "th", "Tiếng Indonesia": "id", "Tiếng Pháp": "fr",
             "Tiếng Tây Ban Nha": "es", "Tiếng Nga": "ru"}
NO_PROMPT = "(Không dùng prompt riêng)"
STEPS = [("audio", "Tách tiếng"), ("listen", "Nghe giọng"), ("translate", "Dịch"),
         ("fix", "Dịch bù"), ("save", "Lưu file")]
JOB_STATE = {"queued": ("⏳ Chờ", "#8a94a7"), "run": ("▶ Đang chạy", "#2fd9ab"),
             "retry": ("↻ Thử lại", "#f2b84b"), "done": ("✔ Xong", "#21c49a"), "fail": ("✖ Lỗi", "#ef5d6c")}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DATA = app_dir() / "data"
PROMPTS = DATA / "prompts"
CONFIG = DATA / "settings.json"


def source_srt_for(inp: str, out_dir: str, source_lang: str) -> str:
    """File phụ đề gốc tạo ra từ video (vd: tap1.zh.srt)."""
    return output_path_for(inp, out_dir, source_lang)


def output_path_for(inp: str, out_dir: str, target_lang: str) -> str:
    p = Path(inp)
    folder = Path(out_dir) if out_dir else p.parent
    return str(folder / f"{p.stem}.{LANG_CODE.get(target_lang, 'out')}.srt")


# ---------------------------------------------------------------- worker
class Worker(QThread):
    log = Signal(str)
    event = Signal(dict)
    progress = Signal(int, int)
    file_progress = Signal(int, int)
    done = Signal(str)

    def __init__(self, mode, files, out_dir, providers, settings, only_transcribe=False):
        super().__init__()
        self.mode, self.files, self.out_dir = mode, files, out_dir
        self.providers, self.settings = providers, settings
        self.only_transcribe = only_transcribe
        self.asr_engine = ""
        self.stop_event = threading.Event()

    def _source_cues(self, path: str) -> list:
        """Phụ đề gốc: đọc file .srt, hoặc nghe giọng từ video (có sẵn .zh.srt thì dùng lại)."""
        if not is_media(path):
            return srt.parse(srt.read_text(path))
        src = source_srt_for(path, self.out_dir, self.settings.source_lang)
        if os.path.exists(src) and (self.mode == "fix" or not self.only_transcribe):
            if self.mode == "fix" or os.path.getmtime(src) >= os.path.getmtime(path):
                self.log.emit(f"  Dùng lại phụ đề gốc đã nghe: {Path(src).name}")
                return srt.parse(srt.read_text(src))
        if self.mode == "fix":
            return []
        tc = Transcriber(self.providers, self.settings.source_lang, self.settings.custom_prompt,
                         threads=min(self.settings.threads, 8), stop=self.stop_event, engine=self.asr_engine,
                         log=self.log.emit, progress=self.progress.emit, event=self.event.emit)
        cues = tc.run(path)
        if self.stop_event.is_set():
            raise Stopped()
        if cues:
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            with open(src, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(srt.compose(cues))
            self.log.emit(f"  ✔ Đã nghe xong {len(cues)} câu → {src}")
        return cues

    def stop(self):
        self.stop_event.set()

    def run(self):
        tr = Translator(self.providers, self.settings, self.stop_event)
        tr.cb.log = self.log.emit
        tr.cb.progress = self.progress.emit
        tr.cb.event = self.event.emit
        ok = 0
        try:
            for fi, path in enumerate(self.files, 1):
                if self.stop_event.is_set():
                    break
                self.file_progress.emit(fi, len(self.files))
                self.event.emit({"type": "file", "name": Path(path).name, "i": fi, "n": len(self.files)})
                self.log.emit(f"━━ [{fi}/{len(self.files)}] {Path(path).name}")
                cues = self._source_cues(path)
                if not cues:
                    self.log.emit("  ✖ Không có câu phụ đề nào (file rỗng hoặc video không có lời thoại)")
                    continue
                if self.only_transcribe and is_media(path) and self.mode == "translate":
                    ok += 1
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
                    self.event.emit({"type": "stage", "stage": "fix", "text": "Đang so file gốc với file đã dịch"})
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
                self.event.emit({"type": "stage", "stage": "save", "text": f"Đang lưu {Path(out).name}"})
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
        self.setFixedHeight(84)
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 12, 18, 12)
        h.setSpacing(14)
        icon = QLabel("⇪"); icon.setObjectName("dropIcon")
        h.addWidget(icon)
        col = QVBoxLayout(); col.setSpacing(2)
        self.title = QLabel("Kéo thả phụ đề .srt hoặc video vào đây"); self.title.setObjectName("dropTitle")
        self.sub = QLabel("Video sẽ được nghe giọng thành phụ đề · chọn nhiều file cho cả bộ phim")
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        for lab in (self.title, self.sub):          # không để chữ dài ép ô (và cả cột) rộng ra
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        col.addWidget(self.title); col.addWidget(self.sub)
        h.addLayout(col, 1)

    def set_files(self, files: list[str]):
        if not files:
            return
        if len(files) == 1:
            self._full = (Path(files[0]).name, str(Path(files[0]).parent))
        else:
            names = ", ".join(Path(f).name for f in files[:3])
            self._full = (f"{len(files)} file phụ đề", names + (" …" if len(files) > 3 else ""))
        self.setToolTip("\n".join(files[:20]))
        self._elide()

    _full: tuple[str, str] | None = None

    def _elide(self):
        """Đường dẫn dài không có dấu cách không xuống dòng được: rút gọn “…” theo bề rộng ô,
        để không kéo giãn cả cột (gây thanh cuộn ngang và cắt mất nội dung bên phải)."""
        if not self._full:
            return
        width = max(120, self.width() - 90)
        for lab, text in zip((self.title, self.sub), self._full):
            lab.setText(lab.fontMetrics().elidedText(text, Qt.ElideMiddle, width))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

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
        files = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.toLocalFile().lower().endswith(".srt") or is_media(u.toLocalFile())]
        if files:
            self.files_dropped.emit(files)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subtitle Translator")
        self.setWindowIcon(theme.app_icon())
        self.resize(1180, 860)
        # màn hình thấp (1366×768…): cửa sổ được nhỏ lại, nội dung từng tab tự cuộn thay vì bị ép
        self.setMinimumSize(980, 560)
        scr = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        if scr is not None:
            self.resize(min(1180, scr.width() - 20), min(860, scr.height() - 30))
        PROMPTS.mkdir(parents=True, exist_ok=True)
        self.cfg = self._load_cfg()
        migrate_models(self.cfg)
        self.worker: Worker | None = None
        self.files: list[str] = []
        self.t0 = 0.0

        # ===== trạng thái chung (góc phải thanh tiêu đề)
        self.pill = QLabel("●  Sẵn sàng"); self.pill.setObjectName("pill")

        # ===== trang Cài đặt AI: thẻ cấu hình một nhà cung cấp
        c1, v1 = card("Cấu hình nhà cung cấp")
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
        self.chk_use = QCheckBox("Dùng nhà cung cấp này khi dịch")
        self.chk_use.setCursor(Qt.PointingHandCursor)
        self.lb_using = QLabel(); self.lb_using.setObjectName("using"); self.lb_using.setWordWrap(True)
        self.base_wrap = QWidget()
        bw = QVBoxLayout(self.base_wrap); bw.setContentsMargins(0, 0, 0, 0)
        bw.addLayout(field("Base URL", self.ed_base))
        v1.addLayout(field("Nhà cung cấp", self.cb_provider))
        v1.addLayout(field("Mô hình", mrow))
        v1.addWidget(self.base_wrap)
        kv = QVBoxLayout(); kv.setSpacing(5)
        khead = QHBoxLayout()
        self.lb_keys = QLabel("API KEY"); self.lb_keys.setObjectName("fieldLabel")
        self.btn_json = btn("Chọn file JSON…", "link", "Chọn file service account .json (chọn nhiều file = xoay vòng)")
        self.lb_free = QLabel(); self.lb_free.setObjectName("hint")
        self.lb_free.setOpenExternalLinks(True); self.lb_free.setTextFormat(Qt.RichText)
        khead.addWidget(self.lb_keys); khead.addStretch(1); khead.addWidget(self.lb_free); khead.addWidget(self.btn_json)
        kv.addLayout(khead); kv.addWidget(self.ed_keys)
        # Vertex service account: Project ID + Location
        self.sa_wrap = QWidget()
        sw = QHBoxLayout(self.sa_wrap); sw.setContentsMargins(0, 0, 0, 0); sw.setSpacing(10)
        self.ed_project = QLineEdit(); self.ed_project.setPlaceholderText("tự đọc từ file JSON")
        self.cb_location = QComboBox(); self.cb_location.setEditable(True)
        self.cb_location.addItems(VERTEX_LOCATIONS)
        sw.addLayout(field("Project ID", self.ed_project), 3)
        sw.addLayout(field("Location", self.cb_location), 2)
        v1.addLayout(kv)
        v1.addWidget(self.sa_wrap)
        self.btn_free = btn("✨ AI miễn phí…", "ghost", "Lấy key miễn phí một lần (Groq, Cerebras, NVIDIA, SiliconFlow, "
                            "Zhipu, Gemini, OpenRouter) — app tự nhận, tự kiểm tra, tự bật")
        trow = QHBoxLayout(); trow.addWidget(self.chk_use); trow.addStretch(1); trow.addWidget(self.btn_free)
        trow.addWidget(self.btn_test)
        v1.addLayout(trow)
        v1.addWidget(self.lb_using)
        self.btn_rm_keys = btn("Xoá toàn bộ key của nhà này", "link", "Xoá key đã lưu trên máy và tắt nhà cung cấp này")
        self.btn_rm_keys.setStyleSheet("color:#ef5d6c")
        rrow = QHBoxLayout(); rrow.addStretch(1); rrow.addWidget(self.btn_rm_keys)
        v1.addLayout(rrow)
        v1.addStretch(1)

        # ===== thẻ 2: Phụ đề
        c2, v2 = card("Phụ đề", "1")
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
        self.chk_only_tc = QCheckBox("Video: chỉ tạo phụ đề gốc, không dịch")
        self.chk_only_tc.setCursor(Qt.PointingHandCursor)
        self.chk_only_tc.setMinimumHeight(24)
        v2.addWidget(self.drop)
        from .hf_local import WHISPER_MODELS
        self.cb_asr = QComboBox()
        self.cb_asr.addItem("Nghe bằng Gemini / Vertex AI", "")
        self.cb_asr.setToolTip("Video được nghe giọng thành phụ đề gốc trước khi dịch.\n"
                               "HF = model Hugging Face chạy trên card của bạn, miễn phí, không cần key")
        for label, mid in WHISPER_MODELS.items():
            self.cb_asr.addItem(f"HF {label} · free", mid)
        i = self.cb_asr.findData(self.cfg.get("asr", ""))
        self.cb_asr.setCurrentIndex(max(0, i))
        self.cb_asr.currentIndexChanged.connect(lambda _: self.cfg.__setitem__("asr", self.cb_asr.currentData() or ""))
        self.chk_only_tc.setText("Chỉ tạo phụ đề gốc")
        arow = QHBoxLayout(); arow.setSpacing(10)
        arow.addWidget(self.cb_asr, 1); arow.addWidget(self.chk_only_tc)
        v2.addLayout(arow)
        v2.addLayout(field("Lưu vào thư mục", orow))
        c2b, v2b = card("Cách dịch", "2")
        v2b.addLayout(lrow)
        v2b.addLayout(field("Prompt riêng cho phim", prow,
                            "Tên nhân vật, xưng hô, giữ từ Hán Việt… — dòng bắt đầu bằng # là ghi chú"))
        self.lb_gloss = QLabel("Chọn phụ đề để dùng bảng thuật ngữ của thư mục phim")
        self.lb_gloss.setObjectName("hint")
        self.btn_gloss = btn("Bảng thuật ngữ…", "link", "Tên nhân vật, chiêu thức, địa danh… dịch thống nhất cả bộ phim")
        self.chk_refine = QCheckBox("Dịch kỹ (thêm lượt biên tập)")
        self.chk_refine.setToolTip("Mỗi batch gửi thêm 1 lần: AI rà lại bản dịch, sửa sai ý/xưng hô/thuật ngữ và viết "
                                   "lại cho tự nhiên. Tốn gấp đôi lượt gọi.")
        grow = QHBoxLayout(); grow.setSpacing(8)
        grow.addWidget(self.lb_gloss, 1); grow.addWidget(self.btn_gloss); grow.addWidget(self.chk_refine)
        v2b.addLayout(field("Thuật ngữ cả bộ phim", grow))

        # ===== thẻ 3: Tốc độ (nâng cao, thu gọn sẵn)
        c3, v3 = card("Tốc độ", "3")
        self.sp_threads = QSpinBox(); self.sp_threads.setRange(1, 64)
        self.sp_rpm = QSpinBox(); self.sp_rpm.setRange(0, 10000)
        self.sp_rpm.setSpecialValueText("Không giới hạn")
        self.sp_batch = QSpinBox(); self.sp_batch.setRange(1, 300)
        for w, tip in ((self.sp_threads, "Số batch gửi cùng lúc"), (self.sp_rpm, "Theo RPM của gói API, để tránh lỗi 429"),
                       (self.sp_batch, "Số câu mỗi lần gửi (30–60 là vừa)")):
            w.setToolTip(tip)
        srow = QHBoxLayout(); srow.setSpacing(14); srow.setAlignment(Qt.AlignTop)
        srow.addLayout(field("Số luồng", self.sp_threads), 1)
        srow.addLayout(field("Request / phút", self.sp_rpm), 1)
        srow.addLayout(field("Batch size", self.sp_batch), 1)
        self.speed_body = QWidget()
        self.speed_body.setLayout(srow); srow.setContentsMargins(0, 0, 0, 0)
        v3.addWidget(self.speed_body)
        self.lb_speed = QLabel(); self.lb_speed.setObjectName("hint")
        self.btn_speed = QToolButton(); self.btn_speed.setObjectName("collapse"); self.btn_speed.setCursor(Qt.PointingHandCursor)
        v3.itemAt(0).layout().addWidget(self.lb_speed); v3.itemAt(0).layout().addWidget(self.btn_speed)
        self.btn_speed.clicked.connect(lambda: self._set_speed_open(not self.speed_body.isVisible()))
        for w in (self.sp_threads, self.sp_rpm, self.sp_batch):
            w.valueChanged.connect(lambda _=0: self._set_speed_open(self.speed_body.isVisible()))

        # ===== thẻ nhật ký
        c4, v4 = card("Nhật ký")
        self.btn_clear = btn("Xóa", "link")
        v4.itemAt(0).layout().addWidget(self.btn_clear)
        self.log_box = QPlainTextEdit(); self.log_box.setObjectName("log")
        self.log_box.setReadOnly(True); self.log_box.setMaximumBlockCount(5000)
        self.log_box.setMinimumHeight(60)
        self.log_box.setPlaceholderText("Nhật ký sẽ hiện ở đây…")
        v4.addWidget(self.log_box, 1)

        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(c2); left.addWidget(c2b); left.addStretch(1)
        right = QVBoxLayout(); right.setSpacing(14)
        right.addWidget(c3); right.addWidget(c4, 1)
        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addLayout(left, 1); cols.addLayout(right, 1)

        # ===== dòng tóm tắt AI đang dùng (cấu hình ở trang Cài đặt AI)
        aibar = QFrame(); aibar.setObjectName("aiBar")
        ab = QHBoxLayout(aibar); ab.setContentsMargins(12, 6, 8, 6); ab.setSpacing(8)
        ai_ic = QLabel(); ai_ic.setPixmap(theme.glyph_pixmap("settings", theme.ACCENT, 16))
        self.lb_ai_short = QLabel(); self.lb_ai_short.setTextFormat(Qt.RichText)
        self.btn_ai_go = btn("Đổi nhà cung cấp AI…", "link", "Mở trang Cài đặt AI")
        ab.addWidget(ai_ic); ab.addWidget(self.lb_ai_short, 1); ab.addWidget(self.btn_ai_go)
        self.aibar = aibar

        # ===== thanh hành động
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 16, 18, 16); av.setSpacing(12)
        self.bar = QProgressBar(); self.bar.setValue(0); self.bar.setFixedHeight(12)
        self.lb_pct = QLabel("0%"); self.lb_pct.setObjectName("cardTitle"); self.lb_pct.setMinimumWidth(48)
        self.lb_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lb_file = QLabel("File 0/0"); self.lb_batch = QLabel("Batch 0/0")
        self.lb_time = QLabel("⏱ 0s")
        brow = QHBoxLayout(); brow.setSpacing(8)
        brow.addWidget(self.bar, 1)
        for w in (self.lb_file, self.lb_batch, self.lb_time):
            w.setObjectName("chip"); brow.addWidget(w)
        brow.addWidget(self.lb_pct)
        self.btn_run = btn("▶   Bắt đầu dịch", "primary")
        self.btn_stop = btn("■  Dừng", "danger")
        self.btn_fix = btn("Check dịch sót", "ghost", "Tìm câu trống / còn chữ gốc trong file đã dịch và dịch lại")
        self.btn_fmt = btn("Check format", "ghost", "Kiểm tra số thứ tự, timecode, dòng trống")
        self.btn_edit_sub = btn("Sửa phụ đề", "ghost", "Mở bảng sửa phụ đề: so bản gốc với bản dịch, xem trước trên video, "
                                "tô đỏ câu trống, câu còn chữ gốc, câu đọc quá nhanh")
        self.btn_edit_sub.setIcon(theme.icon("edit", theme.TEXT, 16))
        self.btn_stop.setEnabled(False)
        # dòng trạng thái (luôn hiện) + nút phóng to / thu nhỏ
        self.lb_stage = QLabel("Chưa chạy. Chọn file rồi bấm “Bắt đầu dịch”.")
        self.lb_stage.setObjectName("stage")
        self.lb_stage.setFixedHeight(40)
        self.lb_stage.setMinimumWidth(200)
        self.lb_stage.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.btn_detail = btn("⤢  Phóng to", "link", "Phóng to: xem chi tiết từng việc đang chạy · Thu nhỏ: quay về màn hình cài đặt")

        srow2 = QHBoxLayout(); srow2.setSpacing(8)
        srow2.addWidget(self.lb_stage, 1); srow2.addWidget(self.btn_detail)
        crow = QHBoxLayout(); crow.setSpacing(8)
        crow.addStretch(1)
        crow.addWidget(self.btn_edit_sub); crow.addWidget(self.btn_fix); crow.addWidget(self.btn_fmt)
        crow.addSpacing(10)
        crow.addWidget(self.btn_stop); crow.addWidget(self.btn_run)

        # bảng chi tiết (ẩn khi thu nhỏ)
        self.detail = QWidget()
        dv = QVBoxLayout(self.detail); dv.setContentsMargins(0, 0, 0, 0); dv.setSpacing(8)
        steps = QHBoxLayout(); steps.setSpacing(6)
        self.step_lbls = {}
        for i, (key, name) in enumerate(STEPS):
            if i:
                arrow = QLabel("›"); arrow.setObjectName("muted"); steps.addWidget(arrow)
            lab = QLabel(name); lab.setObjectName("stepChip"); lab.setProperty("state", "")
            self.step_lbls[key] = lab
            steps.addWidget(lab)
        steps.addStretch(1)
        self.jobs = QTableWidget(0, 5)
        self.jobs.setObjectName("jobs")
        self.jobs.setHorizontalHeaderLabels(["Việc", "Trạng thái", "Nguồn", "Thời gian", "Ghi chú"])
        self.jobs.verticalHeader().setVisible(False)
        self.jobs.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.jobs.setSelectionMode(QAbstractItemView.NoSelection)
        self.jobs.setFocusPolicy(Qt.NoFocus)
        self.jobs.setShowGrid(False)
        self.jobs.setWordWrap(False)
        hh = self.jobs.horizontalHeader()
        for c, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                  QHeaderView.Interactive, QHeaderView.ResizeToContents, QHeaderView.Stretch)):
            hh.setSectionResizeMode(c, mode)
        self.jobs.setColumnWidth(2, 210)
        self.jobs.verticalHeader().setDefaultSectionSize(26)
        self.jobs.setMinimumHeight(150)
        hint = QLabel("Bấm “Thu nhỏ” để quay lại cài đặt và nhật ký. Mỗi dòng là một việc gửi lên AI.")
        hint.setObjectName("hint")
        dv.addLayout(steps); dv.addWidget(self.jobs, 1); dv.addWidget(hint)
        self.detail.setVisible(False)

        av.addLayout(brow); av.addLayout(srow2); av.addWidget(self.detail, 1); av.addLayout(crow)
        self._job_rows: dict[str, int] = {}
        self._job_start: dict[str, float] = {}
        self._job_state: dict[str, str] = {}
        self._job_stage: dict[str, str] = {}
        self._cur_stage = ""
        self._stage_text = ""


        root = QWidget(); root.setObjectName("root")
        cols.setContentsMargins(0, 0, 0, 0)
        settings_w = QWidget(); settings_w.setLayout(cols)
        self.settings_area = self._scroll(settings_w)
        page1 = QWidget()
        p1 = QVBoxLayout(page1); p1.setContentsMargins(0, 0, 0, 0); p1.setSpacing(12)
        p1.addWidget(self.aibar)
        p1.addWidget(self.settings_area, 1)
        p1.addWidget(act)                     # thanh hành động luôn ở đáy, không bị cuộn mất
        self._root_v, self._act = p1, act

        from .render_page import RenderPage
        self.render_page = RenderPage(self.cfg, self._save_cfg, self._log, lambda: self.ed_output.text().strip())
        from .search_page import SearchPage
        self.search_page = SearchPage(self.cfg, self._save_cfg, self._log, self._make_translate_providers)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._scroll(self.search_page))   # 0 · Tìm & tải phim
        self.stack.addWidget(page1)                            # 1 · Dịch phụ đề
        from .dub_page import DubPage
        self.dub_page = DubPage(self.cfg, self._save_cfg, self._log, self._make_translate_providers,
                                lambda: self.ed_output.text().strip(), lambda: self.render_page)
        self.stack.addWidget(self._scroll(self.dub_page))      # 2 · Lồng tiếng
        self.stack.addWidget(self._scroll(self.render_page))   # 3 · Xuất video Vietsub

        # ===== 4 · Cài đặt AI: bảng mọi nhà cung cấp + thẻ cấu hình nhà đang chọn
        self.ai_page = QWidget()
        al = QHBoxLayout(self.ai_page); al.setContentsMargins(0, 0, 0, 0); al.setSpacing(14)
        tcard, tv = card("Nhà cung cấp AI")
        intro = QLabel("Bấm một dòng để cấu hình. Tick <b>Dùng</b> ở nhiều nhà để chạy cùng lúc: "
                       "nhà nào hết lượt hoặc lỗi, app tự chuyển sang nhà khác.")
        intro.setObjectName("hint"); intro.setWordWrap(True)
        self.ai_table = QTableWidget(len(PROVIDERS), 4)
        self.ai_table.setObjectName("grid")
        self.ai_table.setHorizontalHeaderLabels(["Nhà cung cấp", "Trạng thái", "Model", "Dùng"])
        self.ai_table.verticalHeader().setVisible(False)
        self.ai_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ai_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ai_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.ai_table.setShowGrid(False)
        self.ai_table.verticalHeader().setDefaultSectionSize(34)
        ah = self.ai_table.horizontalHeader()
        for c, mode in enumerate((QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Stretch, QHeaderView.Fixed)):
            ah.setSectionResizeMode(c, mode)
        self.ai_table.setColumnWidth(1, 150); self.ai_table.setColumnWidth(3, 52)
        self.ai_table.setWordWrap(False)
        self._ai_pills: dict[str, QLabel] = {}
        self._ai_checks: dict[str, QCheckBox] = {}
        for r, name in enumerate(PROVIDERS):
            it = QTableWidgetItem(name); it.setData(Qt.UserRole, name)
            self.ai_table.setItem(r, 0, it)
            pill = QLabel(); pill.setObjectName("statusPill"); pill.setAlignment(Qt.AlignCenter)
            holder = QWidget(); hl = QHBoxLayout(holder); hl.setContentsMargins(6, 0, 6, 0); hl.addWidget(pill)
            self.ai_table.setCellWidget(r, 1, holder)
            self.ai_table.setItem(r, 2, QTableWidgetItem(""))
            chk = QCheckBox(); chk.setCursor(Qt.PointingHandCursor)
            chk.setToolTip("Dùng nhà cung cấp này khi dịch / nghe giọng / lồng tiếng")
            chk.toggled.connect(lambda on, n=name: self._ai_toggle(n, on))
            ch = QWidget(); cl = QHBoxLayout(ch); cl.setContentsMargins(10, 0, 10, 0); cl.addWidget(chk, 0, Qt.AlignCenter)
            self.ai_table.setCellWidget(r, 3, ch)
            self._ai_pills[name], self._ai_checks[name] = pill, chk
        self.ai_table.itemSelectionChanged.connect(self._ai_row_selected)
        tv.addWidget(intro); tv.addWidget(self.ai_table, 1)
        for r, name in enumerate(PROVIDERS):
            self.ai_table.item(r, 0).setToolTip(name)
        al.addWidget(tcard, 6); al.addWidget(c1, 5)
        self.stack.addWidget(self._scroll(self.ai_page))       # 4 · Cài đặt AI
        from .split_page import SplitPage
        self.split_page = SplitPage(self._log)
        self.stack.addWidget(self.split_page)                  # 5 · Cắt video
        from .auto_page import AutoPage
        self.auto_page = AutoPage(self)
        self.stack.addWidget(self.auto_page)                   # 6 · Tự động từ link
        self.stack.setCurrentIndex(1)

        # ===== thanh điều hướng dọc bên trái (kiểu CapCut)
        side = QFrame(); side.setObjectName("sidebar"); side.setFixedWidth(94)
        sl = QVBoxLayout(side); sl.setContentsMargins(9, 14, 9, 12); sl.setSpacing(4)
        logo = QLabel(); logo.setPixmap(theme.app_icon().pixmap(40, 40)); logo.setAlignment(Qt.AlignCenter)
        logo.setToolTip("Subtitle Translator")
        sl.addWidget(logo); sl.addSpacing(10)
        self.nav_group = QButtonGroup(self)
        self.nav_btns: list = [None] * self.stack.count()

        def section(text):
            lab = QLabel(text); lab.setObjectName("sideSection"); lab.setAlignment(Qt.AlignCenter)
            sl.addSpacing(6); sl.addWidget(lab); sl.addSpacing(2)

        def nav(idx, glyph, text, tip):
            b = QToolButton(); b.setObjectName("navItem"); b.setCheckable(True)
            b.setIcon(theme.nav_icon(glyph)); b.setIconSize(QSize(22, 22))
            b.setText(text); b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setFixedWidth(76); b.setMinimumHeight(58); b.setCursor(Qt.PointingHandCursor)
            self.nav_group.addButton(b, idx); self.nav_btns[idx] = b
            sl.addWidget(b, 0, Qt.AlignHCenter)

        section("QUY TRÌNH")
        nav(0, "download", "Tìm && tải", "Tìm và tải phim Bilibili, Douyin…")
        nav(1, "caption", "Dịch", "Dịch phụ đề / nghe giọng từ video")
        nav(2, "mic", "Lồng tiếng", "Lồng tiếng Việt cho video")
        nav(3, "video", "Xuất video", "In phụ đề vào video (Vietsub)")
        section("CÔNG CỤ")
        nav(6, "bolt", "Tự động", "Dán link → tải → phụ đề → dịch → xuất video, một nút")
        nav(5, "cut", "Cắt video", "Cắt video dài thành nhiều đoạn")
        sl.addStretch(1)
        nav(4, "settings", "Cài đặt AI", "API key và nhà cung cấp AI")

        # ===== tiêu đề trang
        self.lb_page = QLabel(); self.lb_page.setObjectName("pageTitle")
        self.lb_page_sub = QLabel(); self.lb_page_sub.setObjectName("pageSub")
        tcol = QVBoxLayout(); tcol.setSpacing(0); tcol.addWidget(self.lb_page); tcol.addWidget(self.lb_page_sub)
        top = QHBoxLayout(); top.addLayout(tcol); top.addStretch(1); top.addWidget(self.pill, 0, Qt.AlignVCenter)
        main = QVBoxLayout(); main.setContentsMargins(20, 14, 20, 16); main.setSpacing(12)
        main.addLayout(top); main.addWidget(self.stack, 1)
        h = QHBoxLayout(root); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        h.addWidget(side); h.addLayout(main, 1)
        self.nav_group.idClicked.connect(self._go_page)
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
        self.btn_gloss.clicked.connect(self._open_gloss)
        self.btn_edit_prompt.clicked.connect(self._edit_prompt)
        self.btn_del_prompt.clicked.connect(self._delete_prompt)
        self.btn_test.clicked.connect(self._test_conn)
        self.btn_free.clicked.connect(self.open_free_keys)
        self.btn_run.clicked.connect(lambda: self._start("translate"))
        self.btn_fix.clicked.connect(lambda: self._start("fix"))
        self.btn_stop.clicked.connect(self._stop)
        self.btn_fmt.clicked.connect(self._check_format)
        self.btn_edit_sub.clicked.connect(self._edit_subs)
        self.btn_ai_go.clicked.connect(lambda: self._go_page(4))
        self.btn_rm_keys.clicked.connect(self._remove_ai_keys)
        self._ai_timer = QTimer(self); self._ai_timer.setSingleShot(True); self._ai_timer.setInterval(120)
        self._ai_timer.timeout.connect(self._refresh_ai_page)
        self.btn_clear.clicked.connect(self.log_box.clear)
        self.btn_detail.clicked.connect(lambda: self._set_detail(not self.detail.isVisible()))
        self.chk_use.toggled.connect(self._on_use_toggled)
        self.btn_json.clicked.connect(self._pick_json)
        self.ed_keys.textChanged.connect(self._refresh_using)
        self.cb_model.currentTextChanged.connect(self._refresh_using)

        self._apply_cfg()
        self._go_page(int(self.cfg.get("page", 1)) if str(self.cfg.get("page", 1)).isdigit() else 1)

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
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        try:
            return restore_config(raw)
        except Exception:  # noqa: BLE001
            cfg, failed = restore_config_partial(raw)
            ans = QMessageBox.question(
                self, "Không đọc được API key",
                "Không giải mã được API key của: " + ", ".join(failed) + ".\n\n"
                "Thường do file cấu hình được chép từ máy khác hoặc tài khoản Windows khác "
                "(key chỉ mở được trên đúng tài khoản đã lưu).\n\n"
                "Xoá các key này và tiếp tục? Mọi cài đặt khác được giữ nguyên, file cũ được sao lưu "
                "thành settings.json.bak. Sau đó bạn nhập lại key cho các nhà trên.\n\n"
                "Chọn No: app không lưu gì để giữ nguyên file cấu hình.")
            if ans != QMessageBox.Yes:
                self._config_locked = True
                return {}
            try:
                shutil.copy2(CONFIG, CONFIG.with_name("settings.json.bak"))
            except OSError:
                pass
            return cfg

    def _apply_cfg(self):
        c = self.cfg
        self._refresh_prompts(c.get("prompt"))
        self.cb_provider.setCurrentText(c.get("provider", "Gemini"))
        self._on_provider(self.cb_provider.currentText())
        self.ed_output.setText(c.get("output", ""))
        self.sp_threads.setValue(c.get("threads", 10))
        self.sp_rpm.setValue(c.get("rpm", 0))
        self.sp_batch.setValue(c.get("batch", 40))
        self.chk_refine.setChecked(c.get("refine", False))
        self.cb_target.setCurrentText(c.get("target", "Tiếng Việt"))
        self.cb_source.setCurrentText(c.get("source", "Tiếng Trung"))
        self._set_detail(bool(c.get("detail", False)), resize=False)
        self._set_speed_open(bool(c.get("speed_open", False)))

    def _set_speed_open(self, on: bool):
        """Thẻ Tốc độ: thu gọn thì chỉ hiện một dòng tóm tắt."""
        self.speed_body.setVisible(on)
        self.btn_speed.setText("Thu gọn ▴" if on else "Chỉnh ▾")
        rpm = self.sp_rpm.value()
        self.lb_speed.setText("" if on else f"{self.sp_threads.value()} luồng · "
                              f"{f'{rpm} req/phút' if rpm else 'không giới hạn'} · batch {self.sp_batch.value()}")
        self.cfg["speed_open"] = on

    PAGES = {
        0: ("Tìm & tải phim", "Bilibili, Douyin, Tây Qua, Weibo, Tiểu Hồng Thư, AcFun"),
        1: ("Dịch phụ đề", "Dịch file .srt, hoặc nghe giọng từ video rồi dịch bằng AI"),
        2: ("Lồng tiếng", "Biến video tiếng Trung thành video nói tiếng Việt"),
        3: ("Xuất video Vietsub", "In phụ đề vào video, che chữ có sẵn, đổi khung hình"),
        4: ("Cài đặt AI", "API key dùng chung cho dịch, nghe giọng và lồng tiếng"),
        5: ("Cắt video", "Cắt một video dài thành nhiều đoạn bằng ffmpeg, không cần mạng"),
        6: ("Tự động từ link", "Dán link → tải → lấy phụ đề → dịch → xuất video Vietsub, chỉ một nút"),
    }

    def _save_provider_state(self, name: str):
        pc = self.cfg.setdefault("per_provider", {}).setdefault(name, {})
        pc["keys"] = self.ed_keys.toPlainText().strip()
        pc["model"] = self.cb_model.currentText().strip()
        pc["base_url"] = self.ed_base.text().strip()
        pc["enabled"] = self.chk_use.isChecked()
        if PROVIDERS[name]["kind"] == "vertex_sa":
            pc["project"] = self.ed_project.text().strip()
            pc["location"] = self.cb_location.currentText().strip() or "global"

    @staticmethod
    def _scroll(page: QWidget) -> QWidget:
        """Bọc trang trong vùng cuộn: đủ chỗ thì giãn hết, thiếu chỗ thì cuộn chứ không đè lên nhau."""
        from PySide6.QtWidgets import QScrollArea
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setObjectName("pageScroll")
        sa.setStyleSheet("QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget { background: transparent; }")
        sa.setWidget(page)
        return sa

    def _go_page(self, i: int):
        if not 0 <= i < self.stack.count():
            i = 1
        self.stack.setCurrentIndex(i)
        self.nav_btns[i].setChecked(True)
        title, sub = self.PAGES.get(i, ("", ""))
        self.lb_page.setText(title); self.lb_page_sub.setText(sub)
        if i == 4:
            self._refresh_ai_page()
        if i == 6:
            self.auto_page.on_show()
        if self.cfg.get("page") == 3 and i == 2 and getattr(self.render_page, "_dub_sync", None):
            self.dub_page.pull_cover_from_render(self.render_page)
            self.render_page._dub_sync = None
        self.cfg["page"] = i
        media_vids = [f for f in self.files if is_media(f) and Path(f).suffix.lower() not in
                      {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}]
        if i == 2 and not self.dub_page.video and media_vids:
            self.dub_page.set_video(media_vids[0])
        if i == 3 and not self.render_page.video:
            vids = [f for f in self.files if is_media(f) and Path(f).suffix.lower() not in
                    {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}]
            if vids:
                self.render_page.set_video(vids[0])

    def _save_cfg(self):
        if hasattr(self, "render_page"):
            self.render_page.store_state()
        if hasattr(self, "search_page"):
            self.search_page.store_state()
        if hasattr(self, "dub_page"):
            self.dub_page.store_state()
        self._save_provider_state(self.cb_provider.currentText())
        self.cfg.update(
            provider=self.cb_provider.currentText(), output=self.ed_output.text(),
            threads=self.sp_threads.value(), rpm=self.sp_rpm.value(), batch=self.sp_batch.value(),
            target=self.cb_target.currentText(), source=self.cb_source.currentText(),
            prompt=self.cb_prompt.currentText(), detail=self.detail.isVisible(),
            refine=self.chk_refine.isChecked(), speed_open=self.speed_body.isVisible(),
        )
        if hasattr(self, "auto_page"):
            self.auto_page.store_state()
        DATA.mkdir(parents=True, exist_ok=True)
        if getattr(self, "_config_locked", False):
            return
        try:
            payload = json.dumps(protect_config(self.cfg), ensure_ascii=False, indent=2)
            tmp = CONFIG.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, CONFIG)
        except Exception:
            QMessageBox.warning(self, "Lưu cấu hình", "Không thể lưu cấu hình an toàn. File cũ được giữ nguyên.")

    _current_provider = None
    _loading = False

    def _on_provider(self, name: str):
        if self._current_provider and self._current_provider != name:
            self._save_provider_state(self._current_provider)
        self._current_provider = name
        self._loading = True
        p = PROVIDERS[name]
        pc = self.cfg.get("per_provider", {}).get(name, {})
        self.ed_keys.setPlainText(pc.get("keys", ""))
        self.cb_model.clear()
        models = list(dict.fromkeys(pc.get("models_cache", []) or p["models"]))
        self.cb_model.addItems(models)
        self.cb_model.setCurrentText(pc.get("model") or (models[0] if models else ""))
        self.chk_use.blockSignals(True)
        self.chk_use.setChecked(bool(pc.get("enabled")))
        self.chk_use.blockSignals(False)
        sa = p["kind"] == "vertex_sa"
        self.sa_wrap.setVisible(sa)
        self.btn_json.setVisible(sa)
        local = p["kind"] == "hf_local"
        self.ed_keys.setPlaceholderText(
            "Bấm “Chọn file JSON…” (vd: vertex.json)\nNhiều file: mỗi dòng một đường dẫn" if sa else
            "Không cần key: model chạy ngay trên card của bạn, miễn phí, không giới hạn.\n"
            "Cần cài trước bằng cai_long_tieng_gpu.bat. Qwen 7B tự nén 4-bit cho card 8 GB." if local else
            "Dán token Hugging Face (hf_…) tạo ở huggingface.co/settings/tokens\n"
            "Tài khoản free chỉ có ít lượt mỗi tháng" if name.startswith("Hugging Face") else
            "Dán API key…\nNhiều key: mỗi dòng một key, app sẽ xoay vòng")
        self.ed_keys.setEnabled(not local)
        if p.get("free"):
            self.lb_free.setText(f'<a href="{p["signup"]}" style="color:#2fd9ab">Lấy key miễn phí ↗</a>')
            self.lb_free.setToolTip(p.get("note", ""))
            self.ed_keys.setPlaceholderText(f"Dán API key miễn phí…\n{p.get('note', '')}")
        self.lb_free.setVisible(bool(p.get("free")))
        self.lb_keys.setText("FILE SERVICE ACCOUNT (.JSON)" if sa else ("KHÔNG CẦN KEY" if local else
                             ("TOKEN HUGGING FACE" if name.startswith("Hugging Face") else "API KEY")))
        self.ed_project.setText(pc.get("project", ""))
        self.cb_location.setCurrentText(pc.get("location", "global"))
        custom = not p["base_url"] and not sa
        self.base_wrap.setVisible(custom)
        self.ed_base.setText(pc.get("base_url", "") if custom else p["base_url"])
        if sa:
            self.ed_base.setText("")
        self._loading = False
        self._refresh_using()

    # ---------- chọn file
    def _pick_input(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Chọn phụ đề hoặc video", self.cfg.get("last_dir", ""),
            "Phụ đề / video (*.srt " + " ".join("*" + x for x in sorted(MEDIA_EXT)) + ");;"
            "Phụ đề (*.srt);;Video / âm thanh (" + " ".join("*" + x for x in sorted(MEDIA_EXT)) + ");;Tất cả (*.*)")
        self._set_files(files)

    def _set_files(self, files: list[str]):
        if files:
            self.files = files
            self.cfg["last_dir"] = str(Path(files[0]).parent)
            self.drop.set_files(files)
        self._refresh_gloss()

    # ---------- bảng thuật ngữ (dùng chung theo thư mục phim)
    def _gloss_path(self) -> str:
        from .glossary_dialog import glossary_path
        return glossary_path(str(Path(self.files[0]).parent)) if self.files else ""

    def _refresh_gloss(self):
        from .glossary_dialog import read_terms
        p = self._gloss_path()
        if not p:
            self.lb_gloss.setText("Chọn phụ đề để dùng bảng thuật ngữ của thư mục phim")
            return
        n = len(read_terms(p))
        self.lb_gloss.setText(f"✔ {n} thuật ngữ — dùng cho cả thư mục" if n else "Chưa có — bấm “Bảng thuật ngữ…”, AI lập giúp")

    def _open_gloss(self):
        from .glossary_dialog import GlossaryDialog
        if not self.files:
            return QMessageBox.information(self, "Chưa chọn phụ đề", "Kéo thả file phụ đề (hoặc video) của bộ phim trước.")
        srts = [f for f in self.files if f.lower().endswith(".srt")]
        dlg = GlossaryDialog(self._gloss_path(), srts, self._make_translate_providers,
                             self.cb_source.currentText(), self.cb_target.currentText(), self)
        dlg.exec()
        self._refresh_gloss()

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
        if PROVIDERS[self.cb_provider.currentText()]["kind"] == "hf_local":
            keys = ["local"]
        if need_key and not keys:
            raise ProviderError("Chưa nhập API key")
        model = self.cb_model.currentText().strip()
        if need_key and not model:
            raise ProviderError("Chưa chọn mô hình")
        return [make_provider(self.cb_provider.currentText(), k, model, self.ed_base.text(),
                              self.ed_project.text(), self.cb_location.currentText())
                for k in (keys or [""])]

    def _pick_json(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn file service account", "",
                                                "Service account (*.json);;Tất cả (*.*)")
        if not files:
            return
        cur = self._keys()
        self.ed_keys.setPlainText("\n".join(dict.fromkeys(cur + files)))
        if not self.ed_project.text().strip():
            self.ed_project.setText(project_from_json(files[0]))

    def _on_use_toggled(self, on: bool):
        self._refresh_using()

    def _sources(self) -> list[tuple[str, list[str], str, dict]]:
        """Các nguồn sẽ dùng khi dịch: [(tên, keys, model, base_url)].
        Không bật nguồn nào -> chỉ dùng nhà cung cấp đang chọn."""
        cur = self.cb_provider.currentText()
        if cur:
            self._save_provider_state(cur)
        out = []
        for name in PROVIDERS:
            pc = self.cfg.get("per_provider", {}).get(name, {})
            keys = [k.strip() for k in pc.get("keys", "").splitlines() if k.strip()]
            if PROVIDERS[name]["kind"] == "hf_local":
                keys = ["local"]
            if pc.get("enabled") and keys and pc.get("model"):
                out.append((name, keys, pc["model"], pc))
        if not out and cur:
            pc = self.cfg["per_provider"][cur]
            keys = [k.strip() for k in pc.get("keys", "").splitlines() if k.strip()]
            if PROVIDERS[cur]["kind"] == "hf_local":
                keys = ["local"]
            out.append((cur, keys, pc.get("model", ""), pc))
        return out

    def _refresh_using(self, *_):
        if not hasattr(self, "lb_using") or self._current_provider is None or self._loading:
            return
        src = self._sources()
        any_enabled = any(pc.get("enabled") for pc in self.cfg.get("per_provider", {}).values())
        parts = [f"<b>{n.split(' (')[0]}</b> ({len(k)} {'file' if PROVIDERS[n]['kind'] == 'vertex_sa' else 'key'})"
                 for n, k, _, _ in src if k]
        if not parts:
            text = "Khi dịch: chưa có API key nào."
        elif any_enabled:
            text = "Khi dịch sẽ dùng cùng lúc: " + " · ".join(parts)
        else:
            text = "Khi dịch dùng: " + parts[0] + " — tick ô bên trên ở nhiều nhà cung cấp để dùng cùng lúc"
        self.lb_using.setText(text)
        # dòng tóm tắt ở tab Dịch
        if not parts:
            short = ("<span style='color:#f2b84b'>Chưa có API key nào.</span> "
                     "Bấm “Đổi nhà cung cấp AI…” để thêm key (có nhà miễn phí).")
        else:
            short = "AI: " + " · ".join(f"<b>{n.split(' (')[0]}</b> <span style='color:#8a94a7'>{m}</span>"
                                        for n, k, m, _ in src if k)
        if hasattr(self, "lb_ai_short"):
            self.lb_ai_short.setText(short)
        if hasattr(self, "_ai_timer"):
            self._ai_timer.start()

    def _make_translate_providers(self):
        provs = []
        for name, keys, model, pc in self._sources():
            if not keys:
                raise ProviderError(f"{name}: chưa nhập API key")
            if not model:
                raise ProviderError(f"{name}: chưa chọn mô hình")
            provs += [make_provider(name, k, model, pc.get("base_url", ""),
                                    pc.get("project", ""), pc.get("location", "")) for k in keys]
        if not provs:
            raise ProviderError("Chưa nhập API key")
        return provs

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
        from .providers import RETIRED
        models = [m for m in models if m not in RETIRED.get(self.cb_provider.currentText(), {})]  # còn ghi nhưng đã tắt
        cur = self.cb_model.currentText()
        self.cb_model.clear()
        self.cb_model.addItems(models)
        self.cb_model.setCurrentText(cur if cur in models else (models[0] if models else cur))
        self.cfg.setdefault("per_provider", {}).setdefault(
            self.cb_provider.currentText(), {})["models_cache"] = models
        self._log(f"✔ Có {len(models)} model")

    def _remove_ai_keys(self):
        name = self.cb_provider.currentText()
        if QMessageBox.question(self, "Xóa key", f"Xóa toàn bộ key của {name} trên máy?") != QMessageBox.Yes:
            return
        self._save_provider_state(name)
        pc = self.cfg.setdefault("per_provider", {}).setdefault(name, {})
        pc["keys"] = ""
        pc["enabled"] = False
        self._current_provider = None
        self._on_provider(name)
        self._save_cfg()
        self._refresh_ai_page()

    def ai_status(self, name: str) -> tuple[str, str]:
        """(chữ trạng thái, kiểu màu) của một nhà cung cấp."""
        pc = self.cfg.get("per_provider", {}).get(name, {})
        count = len([k for k in pc.get("keys", "").splitlines() if k.strip()])
        if PROVIDERS[name]["kind"] == "hf_local":
            return ("Đang bật · trên máy", "done") if pc.get("enabled") else ("Chạy trên máy", "")
        if pc.get("enabled") and count:
            return f"Đang bật · {count} {'file' if PROVIDERS[name]['kind'] == 'vertex_sa' else 'key'}", "done"
        if pc.get("enabled"):
            return "Bật, thiếu key", "warn"
        return ("Đã có key", "run") if count else ("Chưa có key", "")

    def _refresh_ai_page(self):
        """Cập nhật bảng nhà cung cấp tại chỗ (không tạo lại ô, để bấm ô Dùng không bị mất)."""
        if self._current_provider is None or self._loading:
            return
        self._save_provider_state(self.cb_provider.currentText())
        cur = self.cb_provider.currentText()
        for r, name in enumerate(PROVIDERS):
            pc = self.cfg.get("per_provider", {}).get(name, {})
            text, state = self.ai_status(name)
            pill = self._ai_pills[name]
            pill.setText(text); pill.setProperty("state", state); repolish(pill)
            default = PROVIDERS[name]["models"][0] if PROVIDERS[name]["models"] else ""
            self.ai_table.item(r, 2).setText(pc.get("model") or default)
            chk = self._ai_checks[name]
            chk.blockSignals(True); chk.setChecked(bool(pc.get("enabled"))); chk.blockSignals(False)
            if name == cur and self.ai_table.currentRow() != r:
                self.ai_table.blockSignals(True); self.ai_table.selectRow(r); self.ai_table.blockSignals(False)

    def _ai_row_selected(self):
        r = self.ai_table.currentRow()
        if 0 <= r < self.ai_table.rowCount():
            name = self.ai_table.item(r, 0).data(Qt.UserRole)
            if name != self.cb_provider.currentText():
                self.cb_provider.setCurrentText(name)

    def _ai_toggle(self, name: str, on: bool):
        """Ô Dùng trong bảng: bật/tắt nhà cung cấp mà không cần mở cấu hình."""
        if name == self.cb_provider.currentText():
            self.chk_use.setChecked(on)
        else:
            self.cfg.setdefault("per_provider", {}).setdefault(name, {})["enabled"] = on
            self._refresh_using()
        self._save_cfg()

    def open_free_keys(self):
        """Hộp thoại lấy key miễn phí (dùng chung cho tab Dịch phụ đề và Lồng tiếng)."""
        from .free_keys_dialog import FreeKeysDialog
        cur = self.cb_provider.currentText()
        self._save_provider_state(cur)            # ghi lại ô đang sửa trước khi hộp thoại đổi cfg
        dlg = FreeKeysDialog(self.cfg, self)
        dlg.exec()
        if dlg.changed:
            self._current_provider = None          # nạp lại ô key của nhà cung cấp đang chọn từ cfg
            self._on_provider(cur)
            self._save_cfg()
            n = sum(1 for pc in self.cfg.get("per_provider", {}).values() if pc.get("enabled") and pc.get("keys"))
            self._log(f"✔ Đã bật nhà cung cấp miễn phí. Đang dùng {n} nhà, hết lượt nhà này sẽ tự sang nhà khác.")
        self._refresh_using()
        self._refresh_ai_page()

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
            return QMessageBox.warning(self, "Thiếu file", "Chưa chọn file .srt hoặc video")
        try:
            providers = self._make_translate_providers()
        except ProviderError as e:
            return QMessageBox.warning(self, "Lỗi", str(e))
        self._save_cfg()
        settings = self.translate_settings(self._gloss_path())
        if settings.glossary:
            self._log(f"Bảng thuật ngữ: {len(settings.glossary)} mục")
        names = sorted({p.label.split(" · ")[0] for p in providers})
        self._log(f"Nguồn dịch: {', '.join(names)} — tổng {len(providers)} key")
        self.worker = Worker(mode, list(self.files), self.ed_output.text().strip(), providers, settings,
                             only_transcribe=self.chk_only_tc.isChecked())
        self.worker.asr_engine = self.cb_asr.currentData() or ""
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._on_progress)
        self.worker.event.connect(self._on_event)
        self.worker.file_progress.connect(lambda i, n: self.lb_file.setText(f"File {i}/{n}"))
        self.worker.done.connect(self._on_done)
        self._reset_detail()
        self._set_stage_text("Đang chuẩn bị…")
        self._set_running(True)
        self._set_pct(0)
        self.t0 = time.monotonic()
        self.timer.start(500)
        self.worker.start()

    def translate_settings(self, gloss_path: str = "") -> Settings:
        """Cài đặt dịch đang chọn ở tab Dịch (dùng chung cho tab Dịch và chế độ Tự động)."""
        return Settings(
            source_lang=self.cb_source.currentText(), target_lang=self.cb_target.currentText(),
            threads=self.sp_threads.value(), batch_size=self.sp_batch.value(),
            rpm=self.sp_rpm.value(), custom_prompt=self._prompt_text(),
            glossary=load_glossary(gloss_path) if gloss_path else None,
            refine=self.chk_refine.isChecked(),
        )

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self._log("Đang dừng… (chờ các request đang chạy kết thúc)")

    # ---------- tiến độ chi tiết
    def _set_detail(self, on: bool, resize: bool = True):
        """Phóng to: ẩn cài đặt, thẻ tiến độ chiếm cả cửa sổ. Thu nhỏ: quay lại bình thường."""
        self.detail.setVisible(on)
        self.settings_area.setVisible(not on)
        self._root_v.setStretchFactor(self._act, 1 if on else 0)
        self.btn_detail.setText("⤡  Thu nhỏ" if on else "⤢  Phóng to")

    def _reset_detail(self):
        self.jobs.setRowCount(0)
        self._job_rows.clear(); self._job_start.clear(); self._job_state.clear()
        self._job_stage.clear(); self._cur_stage = ""
        for lab in self.step_lbls.values():
            lab.setProperty("state", ""); repolish(lab)

    def _set_stage_text(self, text: str):
        self._stage_text = text
        self._refresh_stage()

    def _on_event(self, e: dict):
        t = e.get("type")
        if t == "file":
            self._reset_detail()
            self._set_stage_text(f"Bắt đầu file {e['i']}/{e['n']}: {e['name']}")
        elif t == "stage":
            keys = [k for k, _ in STEPS]
            cur = keys.index(e["stage"]) if e["stage"] in keys else -1
            for i, k in enumerate(keys):
                lab = self.step_lbls[k]
                old = lab.property("state")
                new = "now" if i == cur else ("done" if i < cur and old in ("now", "done") else
                                              ("skip" if i < cur else ""))
                if i < cur and old == "":
                    new = "skip"
                lab.setProperty("state", new); repolish(lab)
            self._cur_stage = e["stage"]
            self._set_stage_text(e.get("text", ""))
        elif t == "job":
            self._update_job(e)

    def _update_job(self, e: dict):
        jid = e["id"]
        row = self._job_rows.get(jid)
        if row is None:
            row = self.jobs.rowCount()
            self.jobs.insertRow(row)
            self._job_rows[jid] = row
            self._job_stage[jid] = "listen" if jid.startswith("nghe-") else (
                "fix" if jid.startswith("Bù") else "translate")
            for c in range(5):
                self.jobs.setItem(row, c, QTableWidgetItem(""))
        if e.get("name"):
            self.jobs.item(row, 0).setText(e["name"])
        st = e.get("state")
        if st:
            prev = self._job_state.get(jid)
            self._job_state[jid] = st
            label, color = JOB_STATE.get(st, (st, "#8a94a7"))
            it = self.jobs.item(row, 1)
            it.setText(label); it.setForeground(QColor(color))
            if st == "run" and prev != "run":
                self._job_start[jid] = time.monotonic()
            if st in ("done", "fail") and jid in self._job_start:
                self.jobs.item(row, 3).setText(f"{time.monotonic() - self._job_start[jid]:.0f}s")
        if e.get("src"):
            self.jobs.item(row, 2).setText(e["src"])
        if "info" in e:
            self.jobs.item(row, 4).setText(e["info"])
        if st in ("run", "retry", "fail"):
            self.jobs.scrollToItem(self.jobs.item(row, 0))
        self._refresh_stage()

    def _refresh_stage(self):
        """Dòng trạng thái: giai đoạn + việc đang chạy lâu nhất."""
        now = time.monotonic()
        running = [j for j, s in self._job_state.items() if s in ("run", "retry")]
        for j in running:  # cập nhật cột thời gian cho việc đang chạy
            if j in self._job_start:
                self.jobs.item(self._job_rows[j], 3).setText(f"{now - self._job_start[j]:.0f}s")
        cur = {j: s for j, s in self._job_state.items() if self._job_stage.get(j) == self._cur_stage}
        cnt = {k: sum(1 for s in cur.values() if s == k) for k in JOB_STATE}
        text = self._stage_text
        running_now = self.btn_stop.isEnabled()
        if cur and running_now:
            parts = [f"{cnt['done']}/{len(cur)} xong"]  # noqa
            if cnt["run"]:
                parts.append(f"{cnt['run']} đang gửi")
            if cnt["retry"]:
                parts.append(f"{cnt['retry']} đang thử lại")
            if cnt["queued"]:
                parts.append(f"{cnt['queued']} đang chờ")
            if cnt["fail"]:
                parts.append(f"{cnt['fail']} lỗi")
            text += "  ·  " + ", ".join(parts)
            if running:
                j = min(running, key=lambda x: self._job_start.get(x, now))
            else:
                j = None
            if j:
                r = self._job_rows[j]
                name = self.jobs.item(r, 0).text().split(" · ")[0]
                src = self.jobs.item(r, 2).text().split(" · ")[0]
                waited = now - self._job_start.get(j, now)
                text += f"\n{name}: chờ {src or 'AI'} trả lời {waited:.0f}s"
                if waited > 60:
                    text += " (batch lớn/model suy nghĩ lâu)"
        self.lb_stage.setText(text)

    def _on_progress(self, done, total):
        self.lb_batch.setText(f"Batch {done}/{total}")
        self._set_pct(int(done * 100 / total) if total else 0)

    def _set_pct(self, v: int):
        self.bar.setValue(v)
        self.lb_pct.setText(f"{v}%")

    def _on_done(self, msg):
        self.timer.stop()
        self._tick()
        self._set_running(False)
        self._stage_text = msg
        if msg.startswith("Xong"):
            for lab in self.step_lbls.values():
                if lab.property("state") == "now":
                    lab.setProperty("state", "done"); repolish(lab)
            self._set_pct(100)
            self._set_pill("Hoàn tất", "ok")
        elif msg.startswith("Đã dừng"):
            self._set_pill("Đã dừng")
            for j, st in list(self._job_state.items()):
                if st in ("run", "retry", "queued"):
                    self._update_job({"id": j, "state": "fail", "info": "đã dừng theo yêu cầu"})
        else:
            self._set_pill("Có lỗi", "err")
        self._refresh_stage()
        self._log(f"■ {msg}")

    def _set_running(self, running: bool):
        for w in (self.btn_run, self.btn_fix, self.btn_fmt, self.cb_provider, self.drop):
            w.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if running:
            self._set_pill("Đang dịch…", "run")

    def _tick(self):
        self._refresh_stage()
        s = int(time.monotonic() - self.t0)
        self.lb_time.setText(f"⏱ {s // 60}:{s % 60:02d}" if s >= 60 else f"⏱ {s}s")

    # ---------- check format
    def _edit_subs(self):
        """Mở bảng sửa phụ đề cho file đang chọn (bản dịch cạnh bản gốc, xem trước trên video)."""
        from PySide6.QtWidgets import QInputDialog
        from .sub_editor import SubtitleEditor, find_video
        if not self.files:
            return QMessageBox.warning(self, "Thiếu file", "Chọn file .srt hoặc video ở thẻ Phụ đề trước.")
        f = self.files[0]
        if len(self.files) > 1:
            names = [Path(x).name for x in self.files]
            name, ok = QInputDialog.getItem(self, "Sửa phụ đề", "Chọn file:", names, 0, False)
            if not ok:
                return
            f = self.files[names.index(name)]
        out_dir, target = self.ed_output.text().strip(), self.cb_target.currentText()
        out = output_path_for(f, out_dir, target)
        src = source_srt_for(f, out_dir, self.cb_source.currentText()) if is_media(f) else f
        if not os.path.exists(out):
            if not os.path.exists(src):
                return QMessageBox.information(self, "Sửa phụ đề", "Chưa có phụ đề. Bấm “Bắt đầu dịch” trước.")
            out, src = src, ""                     # chưa dịch: sửa luôn bản gốc
        video = f if is_media(f) else find_video(f)
        dlg = SubtitleEditor(out, src if src and os.path.exists(src) and src != out else "", video,
                             self.render_page._style(), self.cb_target.currentText(), self)
        dlg.saved.connect(lambda p: self._log(f"✔ Đã lưu phụ đề đã sửa: {p}"))
        dlg.exec()

    def _check_format(self):
        if not self.files:
            return QMessageBox.warning(self, "Thiếu file", "Chưa chọn file .srt")
        target = self.cb_target.currentText()
        bad = []
        for f in self.files:
            out = output_path_for(f, self.ed_output.text().strip(), target)
            src = source_srt_for(f, self.ed_output.text().strip(), self.cb_source.currentText())
            path = out if os.path.exists(out) else (src if is_media(f) else f)
            if not os.path.exists(path):
                self._log(f"Check format {Path(f).name}: chưa có file phụ đề")
                continue
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
        if self.auto_page.busy() and QMessageBox.question(
                self, "Đang chạy tự động", "Đang xử lý link tự động. Dừng và thoát?") != QMessageBox.Yes:
            e.ignore()
            return
        if self.split_page.busy() and QMessageBox.question(self, "Cắt video", "Dừng cắt video và thoát?") != QMessageBox.Yes:
            e.ignore()
            return
        if self.render_page.busy() and QMessageBox.question(
                self, "Đang xuất video", "Đang xuất video. Dừng và thoát?") != QMessageBox.Yes:
            e.ignore()
            return
        if self.search_page.busy() and QMessageBox.question(
                self, "Đang tải phim", "Vẫn còn phim đang tải. Dừng và thoát?") != QMessageBox.Yes:
            e.ignore()
            return
        if self.dub_page.busy() and QMessageBox.question(
                self, "Đang lồng tiếng", "Đang lồng tiếng. Dừng và thoát?") != QMessageBox.Yes:
            e.ignore()
            return
        self.auto_page.shutdown()
        self.split_page.shutdown()
        self.render_page.shutdown()
        self.search_page.shutdown()
        self.dub_page.shutdown()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
        try:
            self._save_cfg()
        except OSError:
            pass
        self._leftover_threads = self._drain_threads(5.0)
        e.accept()

    def _bg_threads(self):
        """Mọi luồng nền đang được các trang giữ (đọc font, nạp giọng, tải model, kiểm tra key…)."""
        from .free_keys_dialog import _ORPHANS
        out = list(_ORPHANS)
        for obj in (self, self.render_page, self.dub_page, self.search_page, self.auto_page):
            for v in list(vars(obj).values()):
                if isinstance(v, QThread) and v not in out:
                    out.append(v)
        return out

    def _drain_threads(self, seconds: float):
        """Chờ luồng nền xong (tối đa `seconds`). Huỷ một QThread còn đang chạy làm Qt dừng cưỡng bức tiến trình
        (Windows: 0xC0000409), nên luồng nào còn chạy thì trả về để lúc thoát bỏ qua bước dọn dẹp của Python."""
        deadline = time.monotonic() + seconds
        for t in self._bg_threads():
            if t.isRunning():
                t.wait(max(1, int((deadline - time.monotonic()) * 1000)))
        return [t for t in self._bg_threads() if t.isRunning()]


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
    code = app.exec()
    teardown(app, w, code)
    sys.exit(code)


def migrate_models(cfg: dict) -> list[str]:
    """Đổi model đã bị nhà cung cấp tắt (vd. Groq llama-3.3-70b-versatile) trong cấu hình cũ sang model thay thế."""
    from .providers import RETIRED, current_model
    changed = []
    for name, pc in cfg.get("per_provider", {}).items():
        old = pc.get("model", "")
        new = current_model(name, old)
        if new != old:
            pc["model"] = new
            changed.append(f"{name}: {old} → {new}")
        if pc.get("models_cache"):
            pc["models_cache"] = [m for m in pc["models_cache"] if m not in RETIRED.get(name, {})]
    return changed


def teardown(app, w, code: int = 0):
    """Thoát gọn sau khi cửa sổ đã đóng (cài đặt đã lưu trong closeEvent).
    Nếu còn luồng nền chưa xong (đã chờ 5 s), thoát thẳng để Python không huỷ QThread đang chạy."""
    left = [t for t in getattr(w, "_leftover_threads", []) if t.isRunning()]
    if left:
        sys.stdout.flush(); sys.stderr.flush()
        if sys.platform == "win32":
            # os._exit vẫn chạy phần dọn DLL của Qt khi luồng còn sống -> 0xC0000005; TerminateProcess thì không.
            import ctypes
            k = ctypes.WinDLL("kernel32")
            k.GetCurrentProcess.restype = ctypes.c_void_p
            k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k.TerminateProcess(k.GetCurrentProcess(), code)
        os._exit(code)
    try:
        w.deleteLater()
        app.processEvents()
        if hasattr(app, "shutdown"):
            app.shutdown()
    except Exception:  # noqa: BLE001
        pass
