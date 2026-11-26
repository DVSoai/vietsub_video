"""Bảng thuật ngữ dùng chung cho cả bộ phim (tên nhân vật, xưng hô, địa danh, chiêu thức…).

Lưu ở <thư mục phim>/thuat_ngu.json để mọi tập trong cùng thư mục dùng chung.
AI có thể tự lập bảng từ phụ đề gốc; người dùng sửa lại rồi lưu. Khi dịch, chỉ gửi các thuật ngữ có trong
đoạn đang dịch, và câu nào dịch sai thuật ngữ sẽ được dịch bù."""
from __future__ import annotations

import json
import os
import threading

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

FILE_NAME = "thuat_ngu.json"


def glossary_path(folder: str) -> str:
    return os.path.join(folder, FILE_NAME)


def read_terms(path: str) -> list[dict]:
    try:
        return [t for t in json.load(open(path, encoding="utf-8")).get("terms", []) if t.get("src")]
    except (OSError, ValueError):
        return []


def write_terms(path: str, terms: list[dict]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"terms": terms}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class _Extract(QThread):
    done = Signal(object, str)
    log = Signal(str)

    def __init__(self, providers, texts, src_lang, tgt_lang):
        super().__init__()
        self.args = (providers, texts, src_lang, tgt_lang)
        self.stop = threading.Event()

    def run(self):
        from .translator import extract_glossary
        try:
            p, t, s, g = self.args
            self.done.emit(extract_glossary(p, t, s, g, log=self.log.emit, stop=self.stop), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, f"{type(e).__name__}: {e}")


class GlossaryDialog(QDialog):
    def __init__(self, path: str, srt_files: list[str], providers_getter, src_lang: str, tgt_lang: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bảng thuật ngữ")
        self.path, self.srt_files = path, srt_files
        self.providers_getter, self.src_lang, self.tgt_lang = providers_getter, src_lang, tgt_lang
        self.job: _Extract | None = None

        info = QLabel(f"Dùng chung cho mọi tập trong thư mục:\n{os.path.dirname(path)}")
        info.setObjectName("hint"); info.setWordWrap(True)
        self.tb = QTableWidget(0, 3); self.tb.setObjectName("jobs")
        self.tb.setHorizontalHeaderLabels(["Nguyên văn", "Dịch thống nhất là", "Loại"])
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch); hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed); self.tb.setColumnWidth(2, 130)
        self.tb.verticalHeader().setVisible(False)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.btn_ai = QPushButton("✨ AI tự lập từ phụ đề"); self.btn_ai.setObjectName("primary")
        self.btn_add = QPushButton("Thêm dòng"); self.btn_add.setObjectName("ghost")
        self.btn_del = QPushButton("Xoá dòng đã chọn"); self.btn_del.setObjectName("ghost")
        self.lb_state = QLabel(""); self.lb_state.setObjectName("hint"); self.lb_state.setWordWrap(True)
        row = QHBoxLayout(); row.addWidget(self.btn_ai); row.addWidget(self.btn_add); row.addWidget(self.btn_del)
        row.addStretch(1)
        self.btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.btns.button(QDialogButtonBox.Save).setText("Lưu bảng"); self.btns.button(QDialogButtonBox.Save).setObjectName("primary")
        self.btns.button(QDialogButtonBox.Cancel).setText("Đóng")
        hint = QLabel("Khi dịch, app chỉ gửi những thuật ngữ có trong đoạn đang dịch và tự dịch bù câu nào dùng sai. "
                      "Sửa cột giữa nếu AI đề xuất chưa đúng.")
        hint.setObjectName("hint"); hint.setWordWrap(True)
        v = QVBoxLayout(self); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(10)
        v.addWidget(info); v.addLayout(row); v.addWidget(self.lb_state); v.addWidget(self.tb, 1); v.addWidget(hint)
        v.addWidget(self.btns)
        self.resize(760, 560)

        self.btn_ai.setEnabled(bool(srt_files))
        if not srt_files:
            self.btn_ai.setToolTip("Chọn file phụ đề gốc (.srt) ở tab Dịch phụ đề trước")
        self.btn_ai.clicked.connect(self._ai)
        self.btn_add.clicked.connect(lambda: self._add({"src": "", "vi": "", "loai": ""}, select=True))
        self.btn_del.clicked.connect(self._delete)
        self.btns.accepted.connect(self._save)
        self.btns.rejected.connect(self.reject)
        for t in read_terms(path):
            self._add(t)
        self._count()

    def _add(self, t: dict, select=False):
        r = self.tb.rowCount(); self.tb.insertRow(r)
        for c, k in enumerate(("src", "vi", "loai")):
            self.tb.setItem(r, c, QTableWidgetItem(str(t.get(k, ""))))
        if select:
            self.tb.setCurrentCell(r, 0); self.tb.editItem(self.tb.item(r, 0))
        self._count()

    def _delete(self):
        for r in sorted({i.row() for i in self.tb.selectedIndexes()}, reverse=True):
            self.tb.removeRow(r)
        self._count()

    def _count(self):
        self.lb_state.setText(f"{self.tb.rowCount()} thuật ngữ")

    def terms(self) -> list[dict]:
        out, seen = [], set()
        for r in range(self.tb.rowCount()):
            src = (self.tb.item(r, 0).text() if self.tb.item(r, 0) else "").strip()
            vi = (self.tb.item(r, 1).text() if self.tb.item(r, 1) else "").strip()
            loai = (self.tb.item(r, 2).text() if self.tb.item(r, 2) else "").strip()
            if src and vi and src not in seen:
                seen.add(src); out.append({"src": src, "vi": vi, "loai": loai})
        return out

    def _ai(self):
        from . import srt
        try:
            providers = self.providers_getter()
        except Exception as e:  # noqa: BLE001
            return QMessageBox.warning(self, "Chưa có nhà cung cấp AI", str(e))
        texts = []
        for f in self.srt_files:
            try:
                texts += [c.text for c in srt.parse(srt.read_text(f))]
            except Exception:  # noqa: BLE001
                pass
        if not texts:
            return QMessageBox.information(self, "Không có phụ đề", "Không đọc được câu nào từ các file đã chọn.")
        self.btn_ai.setEnabled(False)
        self.lb_state.setText(f"AI đang đọc {len(texts)} câu của {len(self.srt_files)} file…")
        self.job = _Extract(providers, texts, self.src_lang, self.tgt_lang)
        self.job.log.connect(lambda m: self.lb_state.setText(m.strip()))
        self.job.done.connect(self._ai_done)
        self.job.start()

    def _ai_done(self, terms, err):
        self.btn_ai.setEnabled(True)
        if err:
            self.lb_state.setText(f"✖ {err}")
            return
        have = {t["src"] for t in self.terms()}
        new = [t for t in terms or [] if t["src"] not in have]
        for t in new:
            self._add(t)
        self.lb_state.setText(f"✔ AI đề xuất thêm {len(new)} thuật ngữ — kiểm tra rồi bấm “Lưu bảng”. "
                              f"Tổng {self.tb.rowCount()}.")

    def _save(self):
        write_terms(self.path, self.terms())
        self.accept()

    def done(self, r):
        if self.job and self.job.isRunning():
            self.job.stop.set(); self.job.wait(15000)
        super().done(r)
