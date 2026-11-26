"""Offline video splitting with cancellable FFmpeg processes."""
import math
import re
import subprocess
from pathlib import Path
import imageio_ffmpeg
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QComboBox, QDoubleSpinBox, QProgressBar, QMessageBox)

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
    def __init__(self):
        super().__init__()
        self.worker = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Cắt video thành nhiều đoạn — FFmpeg offline"))
        self.source = QLineEdit()
        self.folder = QLineEdit()
        for label, edit, picker in [("Video gốc", self.source, self.pick_source),
                                     ("Thư mục xuất", self.folder, self.pick_folder)]:
            layout.addWidget(QLabel(label))
            row = QHBoxLayout()
            row.addWidget(edit)
            button = QPushButton("Chọn…")
            button.clicked.connect(picker)
            row.addWidget(button)
            layout.addLayout(row)
        self.mode = QComboBox()
        self.mode.addItems(["Theo thời lượng (giây)", "Theo số đoạn"])
        self.value = QDoubleSpinBox()
        self.value.setRange(1, 10000)
        self.value.setValue(60)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.quality = QComboBox()
        self.quality.addItems(["Cắt chính xác (encode lại)", "Cắt nhanh (gần keyframe, có thể lệch mốc)"])
        for widget in (self.mode, self.value, self.quality):
            layout.addWidget(widget)
        self.start = QPushButton("Bắt đầu cắt")
        self.cancel = QPushButton("Hủy")
        self.cancel.setEnabled(False)
        self.start.clicked.connect(self.run_split)
        self.cancel.clicked.connect(self.shutdown_request)
        layout.addWidget(self.start)
        layout.addWidget(self.cancel)
        self.bar = QProgressBar()
        layout.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def change_mode(self, index):
        self.value.setDecimals(0 if index == 1 else 2)
        self.value.setValue(5 if index == 1 else 60)

    def pick_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "Video gốc", "", "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;All files (*)")
        if path:
            self.source.setText(path)
            if not self.folder.text():
                self.folder.setText(str(Path(path).parent))

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Thư mục xuất")
        if path:
            self.folder.setText(path)

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def shutdown_request(self):
        if self.busy():
            self.worker.requestInterruption()

    def shutdown(self):
        self.shutdown_request()
        if self.worker:
            self.worker.wait()

    def run_split(self):
        if self.busy():
            return
        if not Path(self.source.text()).is_file() or not self.folder.text().strip():
            QMessageBox.warning(self, "Cắt video", "Chọn video và thư mục xuất.")
            return
        self.worker = SplitWorker(self.source.text(), self.folder.text(), self.mode.currentIndex(),
                                  self.value.value(), self.quality.currentIndex() == 1, self)
        self.worker.progress.connect(self.update_progress)
        self.worker.result.connect(self.status.setText)
        self.worker.finished.connect(self.finished)
        self.start.setEnabled(False)
        self.cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self.status.setText("Đang xử lý…")
        self.worker.start()

    def update_progress(self, done, total):
        self.bar.setRange(0, total)
        self.bar.setValue(done)

    def finished(self):
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
        self.worker.deleteLater()
        self.worker = None
