"""Offline video splitting with cancellable FFmpeg processes."""
import math
import os
import re
import subprocess
import sys
from pathlib import Path
import imageio_ffmpeg
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame, QAbstractItemView,
    QHeaderView, QTableWidget, QTableWidgetItem, QFileDialog, QComboBox, QDoubleSpinBox, QProgressBar, QMessageBox)

def ranges(total, mode, value):
    if not math.isfinite(total) or total <= 0 or not math.isfinite(value) or value <= 0:
        raise ValueError("Thời lượng không hợp lệ")
    n = int(value) if mode == 1 else math.ceil(total / value)
    if not 1 <= n <= 10000:
        raise ValueError("Số đoạn phải từ 1 đến 10000")
    step = total / n if mode == 1 else value
    return [(i * step, min(total, (i + 1) * step)) for i in range(n)]

class SplitWorker(QThread):
    progress = Signal(int, int)
    result = Signal(str)
    def __init__(self, source, folder, mode, value, fast, parent=None):
        super().__init__(parent)
        self.source, self.folder, self.mode, self.value, self.fast = source, folder, mode, value, fast

    def execute(self, args):
        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as p:
            try:
                while True:
                    if self.isInterruptionRequested():
                        raise InterruptedError()
                    try:
                        _, err = p.communicate(timeout=.2)
                        return p.returncode, err.decode("utf-8", errors="replace")
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()

    def run(self):
        partial = None
        try:
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            _, info = self.execute([exe, "-hide_banner", "-i", self.source])
            m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
            if not m:
                raise ValueError("Không đọc được thời lượng video")
            h, mnt, sec = map(float, m.groups())
            total = h*3600 + mnt*60 + sec
            parts = ranges(total, self.mode, self.value)
            folder = Path(self.folder)
            folder.mkdir(parents=True, exist_ok=True)
            stem = Path(self.source).stem
            # Unique directory avoids overwriting original or previous exports.
            import tempfile
            dest = Path(tempfile.mkdtemp(prefix=stem[:60]+"_parts_", dir=folder))
            for i, (start, end) in enumerate(parts, 1):
                if self.isInterruptionRequested():
                    raise InterruptedError()
                target = dest / f"part_{i:03d}.mp4"
                partial = dest / f"part_{i:03d}.partial.mp4"
                codec = ["-c", "copy"] if self.fast else ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac"]
                rc, err = self.execute([exe, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", str(start), "-i", self.source, "-t", str(end-start),
                    "-map", "0:v:0", "-map", "0:a:0?", *codec, "-y", str(partial)])
                if rc:
                    raise RuntimeError(err[-1000:])
                partial.rename(target)
                partial = None
                self.progress.emit(i, len(parts))
            self.result.emit(f"Hoàn tất: {dest}")
        except InterruptedError:
            self.result.emit("Đã hủy. Các đoạn đã hoàn tất được giữ lại.")
        except Exception as e:
            self.result.emit(f"Lỗi: {e}")
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)

class SplitPage(QWidget):
    """Tab Cắt video: cùng khuôn với các tab khác (thẻ bên trái, xem trước bên phải, thanh hành động ở đáy)."""

    def __init__(self, log=lambda m: None):
        super().__init__()
        from .gui import DropZone, btn, card, field   # gui đã nạp xong khi tạo trang
        self.worker = None
        self.log = log
        self.total = 0.0
        self.out_dir = ""

        # ---- 1 · Video
        c1, v1 = card("Video", "1")
        self.drop = DropZone()
        self.drop.title.setText("Kéo thả video cần cắt vào đây")
        self.drop.sub.setText("hoặc bấm để chọn · mp4, mkv, mov, webm…")
        self.drop.clicked.connect(self.pick_source)
        self.drop.files_dropped.connect(lambda fs: self.set_source(fs[0]))
        self.source = QLineEdit(); self.source.setReadOnly(True); self.source.setPlaceholderText("Chưa chọn video")
        self.lb_info = QLabel(""); self.lb_info.setObjectName("hint")
        v1.addWidget(self.drop); v1.addWidget(self.source); v1.addWidget(self.lb_info)

        # ---- 2 · Cách cắt
        c2, v2 = card("Cách cắt", "2")
        self.mode = QComboBox()
        self.mode.addItems(["Theo thời lượng mỗi đoạn (giây)", "Theo số đoạn"])
        self.value = QDoubleSpinBox()
        self.value.setRange(1, 10000)
        self.value.setValue(60)
        self.quality = QComboBox()
        self.quality.addItems(["Cắt chính xác (encode lại)", "Cắt nhanh (gần keyframe, có thể lệch mốc)"])
        self.quality.setToolTip("Cắt nhanh không nén lại nên rất nhanh, nhưng mốc cắt rơi vào keyframe gần nhất")
        row = QHBoxLayout(); row.setSpacing(10)
        row.addLayout(field("Chia theo", self.mode), 3); row.addLayout(field("Giá trị", self.value), 2)
        self.folder = QLineEdit(); self.folder.setPlaceholderText("Để trống = cạnh video gốc")
        b_folder = btn("Chọn…", "ghost"); b_folder.clicked.connect(self.pick_folder)
        frow = QHBoxLayout(); frow.setSpacing(8); frow.addWidget(self.folder, 1); frow.addWidget(b_folder)
        v2.addLayout(row)
        v2.addLayout(field("Chế độ cắt", self.quality))
        v2.addLayout(field("Lưu vào thư mục", frow, "Mỗi lần cắt tạo một thư mục con mới, không ghi đè"))
        left = QVBoxLayout(); left.setSpacing(14)
        left.addWidget(c1); left.addWidget(c2); left.addStretch(1)

        # ---- Xem trước các đoạn
        c3, v3 = card("Các đoạn sẽ cắt")
        self.lb_count = QLabel(""); self.lb_count.setObjectName("hint")
        v3.itemAt(0).layout().addWidget(self.lb_count)
        self.table = QTableWidget(0, 4); self.table.setObjectName("grid")
        self.table.setHorizontalHeaderLabels(["Đoạn", "Bắt đầu", "Kết thúc", "Dài"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.empty = QLabel("Chọn video để xem trước các đoạn."); self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignCenter)
        v3.addWidget(self.table, 1); v3.addWidget(self.empty, 1)
        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addLayout(left, 5); cols.addWidget(c3, 6)

        # ---- thanh hành động
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 14, 18, 14); av.setSpacing(10)
        self.bar = QProgressBar(); self.bar.setFixedHeight(12)
        self.status = QLabel("Chọn video rồi bấm “Bắt đầu cắt”."); self.status.setObjectName("stage")
        self.status.setWordWrap(True)
        self.btn_open = btn("Mở thư mục", "ghost"); self.btn_open.clicked.connect(self.open_folder)
        self.btn_open.setEnabled(False)
        self.cancel = btn("■  Dừng", "danger")
        self.cancel.setEnabled(False)
        self.start = btn("✂   Bắt đầu cắt", "primary")
        brow = QHBoxLayout(); brow.setSpacing(8)
        brow.addWidget(self.status, 1); brow.addWidget(self.btn_open); brow.addWidget(self.cancel); brow.addWidget(self.start)
        av.addWidget(self.bar); av.addLayout(brow)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addLayout(cols, 1); root.addWidget(act)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.value.valueChanged.connect(lambda _=0: self.refresh_preview())
        self.start.clicked.connect(self.run_split)
        self.cancel.clicked.connect(self.shutdown_request)
        self.refresh_preview()

    # ------------------------------------------------------------
    def change_mode(self, index):
        self.value.setDecimals(0 if index == 1 else 2)
        self.value.setValue(5 if index == 1 else 60)
        self.refresh_preview()

    def pick_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "Video gốc", "",
                                              "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv);;All files (*)")
        if path:
            self.set_source(path)

    def set_source(self, path: str):
        self.source.setText(path)
        self.drop.set_files([path])
        self.total = 0.0
        try:
            from .render import probe
            info = probe(path)
            self.total = float(info.get("duration") or 0)
            self.lb_info.setText(f"{info.get('w', '?')}×{info.get('h', '?')} · dài {_fmt(self.total)}")
        except Exception as e:  # noqa: BLE001
            self.lb_info.setText(f"✖ Không đọc được video: {e}")
        self.refresh_preview()

    def refresh_preview(self):
        self.table.setRowCount(0)
        parts = []
        if self.total > 0:
            try:
                parts = ranges(self.total, self.mode.currentIndex(), self.value.value())
            except ValueError as e:
                self.lb_count.setText(f"✖ {e}")
        self.table.setVisible(bool(parts)); self.empty.setVisible(not parts)
        if not parts:
            if self.total <= 0:
                self.lb_count.setText("")
            return
        self.lb_count.setText(f"{len(parts)} đoạn")
        show = parts[:500]
        self.table.setRowCount(len(show))
        for r, (a, b) in enumerate(show):
            for c, txt in enumerate((f"part_{r + 1:03d}.mp4", _fmt(a), _fmt(b), _fmt(b - a))):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, it)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Thư mục xuất")
        if path:
            self.folder.setText(path)

    def open_folder(self):
        if self.out_dir and os.path.isdir(self.out_dir):
            if sys.platform == "win32":
                os.startfile(self.out_dir)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", self.out_dir])

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def shutdown_request(self):
        if self.busy():
            self.worker.requestInterruption()
            self.status.setText("Đang dừng…")

    def shutdown(self):
        self.shutdown_request()
        if self.worker:
            self.worker.wait()

    def run_split(self):
        if self.busy():
            return
        src = self.source.text()
        if not Path(src).is_file():
            QMessageBox.warning(self, "Cắt video", "Chọn video cần cắt trước.")
            return
        folder = self.folder.text().strip() or str(Path(src).parent)
        self.worker = SplitWorker(src, folder, self.mode.currentIndex(),
                                  self.value.value(), self.quality.currentIndex() == 1, self)
        self.worker.progress.connect(self.update_progress)
        self.worker.result.connect(self.on_result)
        self.worker.finished.connect(self.finished)
        self.start.setEnabled(False)
        self.cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self.status.setText("Đang cắt…")
        self.log(f"━━ Cắt video: {Path(src).name}")
        self.worker.start()

    def on_result(self, text: str):
        self.status.setText(text)
        self.log("  " + text)
        if text.startswith("Hoàn tất: "):
            self.out_dir = text[len("Hoàn tất: "):]
            self.btn_open.setEnabled(True)

    def update_progress(self, done, total):
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.status.setText(f"Đã cắt {done}/{total} đoạn…")

    def finished(self):
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
        self.worker.deleteLater()
        self.worker = None


def _fmt(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
