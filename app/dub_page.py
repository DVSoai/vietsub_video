"""Tab “Lồng tiếng”: video -> tách giọng -> nghe giọng -> dịch kiểu lồng tiếng -> nhân vật & giọng
-> đọc từng câu (VieNeu) -> trộn với nhạc nền -> xuất video."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import srt
from .dub import (
    Character, Line, MixOptions, Project, Stopped, TTSEngine, analyze_characters, auto_assign_voices, encode_m4a,
    fit_lines, line_cache_path, lines_from_srt, mix, mux_video, synth_lines, workdir_for,
)
from .transcribe import MEDIA_EXT

VIDEO_EXT = {e for e in MEDIA_EXT if e not in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}}
STEPS = [("sep", "Tách giọng"), ("asr", "Nghe giọng"), ("tr", "Dịch lồng tiếng"),
         ("chars", "Nhân vật & giọng"), ("tts", "Đọc giọng"), ("mix", "Trộn")]


def _card(t, s=None):
    from .gui import card
    return card(t, s)


def _field(l, w, h=None):
    from .gui import field
    return field(l, w, h)


def _btn(t, k=None, tip=None):
    from .gui import btn
    return btn(t, k, tip)


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ====================================================================== luồng làm việc
class DubWorker(QThread):
    log = Signal(str)
    stage = Signal(str, str)            # key bước, mô tả
    progress = Signal(int, int)
    review = Signal()                   # đã có nhân vật -> chờ người dùng kiểm tra giọng
    done = Signal(str, str)             # kết quả / lỗi

    def __init__(self, page: "DubPage", mode: str, only: set[int] | None = None):
        super().__init__()
        self.p, self.mode, self.only = page, mode, only
        self.stop_event = threading.Event()
        # chụp tuỳ chọn lúc bấm chạy (luồng nền không đọc thẳng widget)
        self.o = {"remove": page.rb_remove.isChecked(), "retr": page.chk_retr.isChecked(),
                  "skip_review": page.chk_skip_review.isChecked(), "export": page.chk_export.isChecked(),
                  "mix": page.mix_options(), "hints": page.hints()}

    def stop(self):
        self.stop_event.set()

    # ---------------------------------------------------------------- các bước
    def _sep(self):
        p = self.p
        inst, voc = os.path.join(p.workdir, "instrumental.wav"), os.path.join(p.workdir, "vocals.wav")
        if os.path.exists(inst) and os.path.exists(voc):
            self.log.emit("  Đã có nhạc nền/giọng tách sẵn, dùng lại")
            return
        from .vocal import separate_file
        self.stage.emit("sep", "Đang tách giọng nói khỏi nhạc nền…")
        t0 = time.monotonic()
        dev = separate_file(p.video, inst, voc, log=self.log.emit,
                            progress=lambda d, t: self.progress.emit(d, t), stop=self.stop_event)
        self.log.emit(f"  ✔ Tách giọng xong ({dev}, {_fmt(time.monotonic() - t0)})")

    def _asr(self) -> str:
        p = self.p
        zh = p.find_srt(p.src_code)
        if zh:
            self.log.emit(f"  Dùng phụ đề gốc có sẵn: {Path(zh).name}")
            return zh
        from .transcribe import Transcriber
        self.stage.emit("asr", "Gemini đang nghe giọng (kèm người nói, giới tính, cảm xúc)…")
        voc = os.path.join(p.workdir, "vocals.wav")
        tc = Transcriber(p.providers(), p.src_lang, self.o['hints'], threads=6, stop=self.stop_event,
                         log=self.log.emit, progress=self.progress.emit, speakers=True)
        cues = tc.run(voc if os.path.exists(voc) else p.video)
        if self.stop_event.is_set():
            raise Stopped()
        if not cues:
            raise RuntimeError("Không nghe được câu thoại nào")
        zh = os.path.join(p.workdir, f"{Path(p.video).stem}.{p.src_code}.srt")
        with open(zh, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(srt.compose(cues))
        meta = {str(c.index): getattr(c, "meta", {}) for c in cues if getattr(c, "meta", None)}
        json.dump(meta, open(os.path.join(p.workdir, "speakers.json"), "w", encoding="utf-8"), ensure_ascii=False)
        self.log.emit(f"  ✔ Nghe được {len(cues)} câu → {Path(zh).name}")
        return zh

    def _translate(self, zh: str) -> str:
        p = self.p
        vi = os.path.join(p.workdir, f"{Path(p.video).stem}.vi.srt")
        if os.path.exists(vi) and not self.o['retr']:
            self.log.emit(f"  Dùng bản dịch lồng tiếng có sẵn: {Path(vi).name}")
            return vi
        from .translator import Settings, Translator
        self.stage.emit("tr", "Đang dịch kiểu lồng tiếng (vừa thời lượng nói)…")
        cues = srt.parse(srt.read_text(zh))
        from .dub import ts_seconds
        durs = {str(c.index): max(0.6, ts_seconds(c.end) - ts_seconds(c.start)) for c in cues}
        st = Settings(source_lang=p.src_lang, target_lang="Tiếng Việt", threads=6, batch_size=25,
                      custom_prompt=self.o['hints'], dub_durations=durs)
        tr = Translator(p.providers(), st, self.stop_event)
        tr.cb.log = self.log.emit
        tr.cb.progress = self.progress.emit
        res = tr.translate_cues(cues)
        out = [srt.Cue(c.index, c.start, c.end, res.get(c.index) or c.text, c.extra) for c in cues]
        with open(vi, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(srt.compose(out))
        self.log.emit(f"  ✔ Dịch xong → {Path(vi).name}")
        return vi

    def _chars(self, zh: str, vi: str):
        p = self.p
        spk = {}
        sp = os.path.join(p.workdir, "speakers.json")
        if os.path.exists(sp):
            spk = json.load(open(sp, encoding="utf-8"))
        proj = p.project
        proj.lines = lines_from_srt(vi, zh, spk)
        self.stage.emit("chars", "Đang nhận diện nhân vật…")
        chars, assign = analyze_characters(p.providers()[0], proj.lines, log=self.log.emit, stop=self.stop_event)
        for l in proj.lines:
            sp_, emo = assign.get(l.index, ("", ""))
            l.speaker = sp_ or l.speaker or "Người dẫn chuyện"
            l.emotion = emo or l.emotion
        names = {c.name for c in chars}
        for l in proj.lines:
            if l.speaker not in names:
                g = l.gender_hint if l.gender_hint in ("nam", "nữ") else ""
                chars.append(Character(l.speaker, g)); names.add(l.speaker)
        proj.characters = chars
        self.stage.emit("chars", "Đang nạp danh sách giọng VieNeu…")
        auto_assign_voices(proj.characters, p.engine.voices())
        proj.save()
        self.log.emit(f"  ✔ {len(chars)} nhân vật: " + ", ".join(c.name for c in chars[:12]))

    def _tts_and_mix(self):
        p = self.p
        proj = p.project
        cache = os.path.join(p.workdir, "tts")
        if self.only:
            for l in proj.lines:
                if l.index in self.only:
                    path = line_cache_path(proj, l, cache)
                    if os.path.exists(path):
                        os.remove(path)
        self.stage.emit("tts", f"VieNeu đang đọc {len(proj.lines)} câu…")
        t0 = time.monotonic()
        wavs = synth_lines(p.engine, proj, cache, log=self.log.emit, stop=self.stop_event,
                           progress=lambda d, t: (self.progress.emit(d, t),
                                                  self.stage.emit("tts", f"VieNeu đang đọc câu {d}/{t} "
                                                                         f"({p.engine.device or 'đang nạp'})…")))
        self.log.emit(f"  ✔ Đọc xong {len(wavs)} câu ({_fmt(time.monotonic() - t0)})")
        self.stage.emit("mix", "Đang khớp thời gian và trộn với nhạc nền…")
        sr = p.engine.sr
        import copy
        opt = copy.copy(self.o['mix'])
        from .vocal import load_audio, save_wav
        inst = os.path.join(p.workdir, "instrumental.wav")
        if opt.remove_original and os.path.exists(inst):
            bg = load_audio(inst, sr=sr)
        else:
            if opt.remove_original:
                self.log.emit("  ⚠ Chưa có nhạc nền đã tách -> dùng tiếng gốc vặn nhỏ")
                opt.remove_original = False
            bg = load_audio(p.video, sr=sr)
        fitted = fit_lines(proj.lines, wavs, opt.max_speed, sr, log=self.log.emit)
        out = mix(proj.lines, fitted, bg, opt, sr)
        tmp = os.path.join(p.workdir, "dub_mix.wav")
        save_wav(tmp, out, sr)
        encode_m4a(tmp, p.dub_audio_path())
        os.remove(tmp)
        self.log.emit(f"  ✔ Đã trộn → {p.dub_audio_path()}")

    # ---------------------------------------------------------------- chạy
    def run(self):
        try:
            p = self.p
            os.makedirs(p.workdir, exist_ok=True)
            if self.mode == "auto":
                need_sep = self.o['remove'] or not p.find_srt(p.src_code)
                if need_sep:
                    self._sep()
                zh = self._asr()
                vi = self._translate(zh)
                self._chars(zh, vi)
                if not self.o['skip_review']:
                    self.done.emit("review", "")
                    return
                self._tts_and_mix()
            elif self.mode == "voice":
                if self.o['remove']:
                    self._sep()
                self._tts_and_mix()
            elif self.mode == "export":
                pass
            if (self.mode in ("auto", "voice") and self.o['export']) or self.mode == "export":
                self.stage.emit("mix", "Đang ghép tiếng lồng vào video…")
                out = p.dub_video_path()
                mux_video(p.video, p.dub_audio_path(), out)
                self.log.emit(f"  ✔ Video lồng tiếng: {out}")
                self.done.emit(out, "")
                return
            self.done.emit(p.dub_audio_path(), "")
        except Stopped:
            self.done.emit("", "Đã dừng")
        except Exception as e:  # noqa: BLE001
            import traceback
            self.log.emit(traceback.format_exc()[-1500:])
            self.done.emit("", f"{type(e).__name__}: {e}")


class _Job(QThread):
    result = Signal(object, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit(None, str(e))


# ====================================================================== trang
class DubPage(QWidget):
    def __init__(self, cfg: dict, save_cfg, log, providers_getter, out_dir_getter):
        super().__init__()
        self.cfg, self.save_cfg, self.main_log = cfg, save_cfg, log
        self.providers = providers_getter
        self.out_dir_getter = out_dir_getter
        self.engine = TTSEngine(log=self._log_any)
        self.video = ""
        self.workdir = ""
        self.project = Project("", "")
        self.voice_list: list[tuple[str, str]] = []
        self.worker: DubWorker | None = None
        self._player = None
        self._build()
        self._load_state()

    # helpers cho worker
    src_lang = "Tiếng Trung"
    src_code = "zh"

    def hints(self) -> str:
        return self.ed_hints.text().strip()

    def mix_options(self) -> MixOptions:
        return MixOptions(remove_original=self.rb_remove.isChecked(), duck=self.sl_duck.value() / 100,
                          voice_gain=self.sl_voice.value() / 100, bg_gain=self.sl_bg.value() / 100,
                          max_speed=self.sp_speed.value())

    def dub_audio_path(self) -> str:
        p = Path(self.video)
        return str(Path(self.workdir).parent / f"{p.stem}.vi.dub.m4a")

    def dub_video_path(self) -> str:
        p = Path(self.video)
        return str(Path(self.workdir).parent / f"{p.stem}.long_tieng.mp4")

    def find_srt(self, code: str) -> str:
        p = Path(self.video)
        dirs = [Path(self.workdir), p.parent]
        od = self.out_dir_getter()
        if od:
            dirs.append(Path(od))
        for d in dirs:
            f = d / f"{p.stem}.{code}.srt"
            if f.exists():
                return str(f)
        return ""

    def _log_any(self, msg: str):
        # có thể được gọi từ luồng nền -> đi qua worker nếu đang chạy
        if self.worker and self.worker.isRunning():
            self.worker.log.emit(msg)
        else:
            QTimer.singleShot(0, lambda: self.main_log(msg))

    # ================================================================ giao diện
    def _build(self):
        from .gui import DropZone, repolish
        self._repolish = repolish

        # --- cột trái: video + tuỳ chọn
        c1, v1 = _card("Video cần lồng tiếng", "1")
        self.drop = DropZone()
        self.drop.title.setText("Kéo thả video vào đây")
        self.drop.sub.setText("App tự dùng phụ đề .zh.srt / .vi.srt cùng tên nếu đã có")
        self.lb_files = QLabel("Chưa chọn video"); self.lb_files.setObjectName("hint"); self.lb_files.setWordWrap(True)
        v1.addWidget(self.drop); v1.addWidget(self.lb_files)

        c2, v2 = _card("Âm thanh", "2")
        self.rb_remove = QRadioButton("Bỏ giọng gốc, giữ nhạc nền && tiếng động (tách giọng AI)")
        self.rb_duck = QRadioButton("Giữ tiếng gốc, vặn nhỏ khi có thoại (kiểu thuyết minh)")
        self.rb_remove.setChecked(True)
        self.sl_duck = QSlider(Qt.Horizontal); self.sl_duck.setRange(0, 80); self.sl_duck.setValue(20)
        self.sl_voice = QSlider(Qt.Horizontal); self.sl_voice.setRange(30, 200); self.sl_voice.setValue(100)
        self.sl_bg = QSlider(Qt.Horizontal); self.sl_bg.setRange(0, 150); self.sl_bg.setValue(90)
        self.sp_speed = QDoubleSpinBox(); self.sp_speed.setRange(1.0, 1.8); self.sp_speed.setSingleStep(0.05)
        self.sp_speed.setValue(1.35); self.sp_speed.setSuffix(" x")
        self.lb_duck = QLabel(); self.lb_voice = QLabel(); self.lb_bg = QLabel()
        for lb in (self.lb_duck, self.lb_voice, self.lb_bg):
            lb.setObjectName("hint"); lb.setMinimumWidth(40)

        def srow(sl, lb):
            r = QHBoxLayout(); r.addWidget(sl, 1); r.addWidget(lb); return r
        v2.addWidget(self.rb_remove); v2.addWidget(self.rb_duck)
        self.duck_wrap = QWidget(); dw = QVBoxLayout(self.duck_wrap); dw.setContentsMargins(0, 0, 0, 0)
        dw.addLayout(_field("Tiếng gốc khi có thoại", srow(self.sl_duck, self.lb_duck)))
        v2.addWidget(self.duck_wrap)
        v2.addLayout(_field("Âm lượng giọng Việt", srow(self.sl_voice, self.lb_voice)))
        v2.addLayout(_field("Âm lượng nhạc nền", srow(self.sl_bg, self.lb_bg)))
        v2.addLayout(_field("Đọc nhanh tối đa", self.sp_speed, "Câu dài hơn khoảng thời gian sẽ được đọc nhanh lên, tối đa mức này"))

        c3, v3 = _card("Tuỳ chọn", "3")
        self.ed_hints = QLineEdit(); self.ed_hints.setPlaceholderText("Tên nhân vật/thuật ngữ, vd: 林澈 = Lâm Triệt; 师尊 = sư tôn")
        self.chk_retr = QCheckBox("Dịch lại kiểu lồng tiếng (kể cả khi đã có .vi.srt)")
        self.chk_skip_review = QCheckBox("Không dừng để kiểm tra giọng (chạy một mạch)")
        self.chk_export = QCheckBox("Xong thì xuất luôn video lồng tiếng (.mp4)")
        self.chk_export.setChecked(True)
        v3.addLayout(_field("Gợi ý tên riêng", self.ed_hints))
        v3.addWidget(self.chk_retr); v3.addWidget(self.chk_skip_review); v3.addWidget(self.chk_export)
        self.lb_engine = QLabel("VieNeu-TTS: chưa nạp"); self.lb_engine.setObjectName("hint"); self.lb_engine.setWordWrap(True)
        v3.addWidget(self.lb_engine)

        from PySide6.QtWidgets import QScrollArea
        inner = QWidget(); left = QVBoxLayout(inner); left.setSpacing(14); left.setContentsMargins(0, 0, 8, 0)
        left.addWidget(c1); left.addWidget(c2); left.addWidget(c3); left.addStretch(1)
        lw = QScrollArea(); lw.setWidgetResizable(True); lw.setWidget(inner); lw.setFixedWidth(410)
        lw.setFrameShape(QFrame.NoFrame); lw.setObjectName("scroll")
        lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # --- cột phải: bước + bảng
        cr, vr = _card("Nhân vật, giọng & câu thoại", None)
        steps = QHBoxLayout(); steps.setSpacing(6)
        self.step_lbls = {}
        for i, (k, name) in enumerate(STEPS):
            if i:
                a = QLabel("›"); a.setObjectName("muted"); steps.addWidget(a)
            lab = QLabel(name); lab.setObjectName("stepChip"); lab.setProperty("state", "")
            self.step_lbls[k] = lab; steps.addWidget(lab)
        steps.addStretch(1)
        vr.addLayout(steps)
        self.tabs = QTabWidget(); self.tabs.setObjectName("dubTabs")
        # bảng nhân vật
        self.tb_chars = QTableWidget(0, 5); self.tb_chars.setObjectName("jobs")
        self.tb_chars.setHorizontalHeaderLabels(["Nhân vật", "Giới tính", "Vai trò / chất giọng", "Giọng VieNeu", ""])
        self._table_style(self.tb_chars, [QHeaderView.ResizeToContents, QHeaderView.Fixed,
                                          QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed])
        self.tb_chars.setColumnWidth(1, 90); self.tb_chars.setColumnWidth(3, 250); self.tb_chars.setColumnWidth(4, 110)
        # bảng câu
        self.tb_lines = QTableWidget(0, 6); self.tb_lines.setObjectName("jobs")
        self.tb_lines.setHorizontalHeaderLabels(["#", "Thời gian", "Nhân vật", "Lời Việt (sửa được)", "Lời gốc", ""])
        self._table_style(self.tb_lines, [QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                          QHeaderView.Interactive, QHeaderView.Stretch, QHeaderView.Interactive,
                                          QHeaderView.ResizeToContents])
        self.tb_lines.setColumnWidth(2, 150); self.tb_lines.setColumnWidth(4, 200)
        self.tb_lines.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tb_lines.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tb_lines.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.tabs.addTab(self.tb_chars, "Nhân vật && giọng")
        self.tabs.addTab(self.tb_lines, "Câu thoại")
        vr.addWidget(self.tabs, 1)
        hint = QLabel("Đổi giọng ở bảng Nhân vật; sửa chữ ở bảng Câu thoại (bấm đúp). Chọn vài câu rồi bấm "
                      "“Tạo lại câu đã chọn” để chỉ đọc lại những câu đó.")
        hint.setObjectName("hint"); hint.setWordWrap(True); vr.addWidget(hint)

        cols = QHBoxLayout(); cols.setSpacing(14)
        cols.addWidget(lw); cols.addWidget(cr, 1)

        # --- thanh hành động
        act = QFrame(); act.setObjectName("card")
        av = QVBoxLayout(act); av.setContentsMargins(18, 14, 18, 14); av.setSpacing(10)
        self.bar = QProgressBar(); self.bar.setFixedHeight(12)
        self.lb_pct = QLabel("0%"); self.lb_pct.setObjectName("cardTitle"); self.lb_pct.setMinimumWidth(48)
        self.lb_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lb_time = QLabel("⏱ 0s"); self.lb_time.setObjectName("chip")
        b = QHBoxLayout(); b.addWidget(self.bar, 1); b.addWidget(self.lb_time); b.addWidget(self.lb_pct)
        self.lb_stage = QLabel("Chọn video rồi bấm “Làm tự động”. App sẽ dừng ở bước Nhân vật & giọng để bạn kiểm tra.")
        self.lb_stage.setObjectName("stage"); self.lb_stage.setWordWrap(True)
        self.btn_auto = _btn("▶  Làm tự động", "primary")
        self.btn_voice = _btn("Đọc giọng && trộn", "ghost", "Sau khi kiểm tra nhân vật/giọng: đọc tất cả câu rồi trộn")
        self.btn_regen = _btn("Tạo lại câu đã chọn", "ghost")
        self.btn_export = _btn("Xuất video lồng tiếng", "ghost")
        self.btn_stop = _btn("■  Dừng", "danger"); self.btn_stop.setEnabled(False)
        self.btn_open = _btn("Mở thư mục", "link")
        c = QHBoxLayout(); c.setSpacing(8)
        c.addWidget(self.lb_stage, 1); c.addWidget(self.btn_open); c.addWidget(self.btn_regen)
        c.addWidget(self.btn_export); c.addWidget(self.btn_voice); c.addWidget(self.btn_stop); c.addWidget(self.btn_auto)
        av.addLayout(b); av.addLayout(c)

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addLayout(cols, 1); root.addWidget(act)

        # --- sự kiện
        self.drop.clicked.connect(self._pick_video)
        self.drop.files_dropped.connect(lambda fs: fs and self.set_video(fs[0]))
        self.btn_auto.clicked.connect(lambda: self._run("auto"))
        self.btn_voice.clicked.connect(lambda: self._run("voice"))
        self.btn_regen.clicked.connect(self._regen)
        self.btn_export.clicked.connect(lambda: self._run("export"))
        self.btn_stop.clicked.connect(lambda: self.worker and self.worker.stop())
        self.btn_open.clicked.connect(self._open_dir)
        for sl, lb, unit in ((self.sl_duck, self.lb_duck, "%"), (self.sl_voice, self.lb_voice, "%"), (self.sl_bg, self.lb_bg, "%")):
            sl.valueChanged.connect(lambda v, lb=lb, unit=unit: lb.setText(f"{v}{unit}"))
            lb.setText(f"{sl.value()}%")
        self.rb_remove.toggled.connect(lambda on: self.duck_wrap.setVisible(not on))
        self.duck_wrap.setVisible(False)
        self.tb_lines.itemChanged.connect(self._line_edited)
        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)

    def _table_style(self, t: QTableWidget, modes):
        t.verticalHeader().setVisible(False)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.verticalHeader().setDefaultSectionSize(34)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = t.horizontalHeader()
        for i, m in enumerate(modes):
            hh.setSectionResizeMode(i, m)

    # ================================================================ trạng thái
    def _load_state(self):
        st = self.cfg.get("dub", {})
        self.rb_duck.setChecked(not st.get("remove", True)); self.rb_remove.setChecked(st.get("remove", True))
        self.sl_duck.setValue(st.get("duck", 20)); self.sl_voice.setValue(st.get("voice", 100))
        self.sl_bg.setValue(st.get("bg", 90)); self.sp_speed.setValue(st.get("speed", 1.35))
        self.chk_export.setChecked(st.get("export", True)); self.chk_skip_review.setChecked(st.get("skip_review", False))

    def store_state(self):
        self.cfg["dub"] = {"remove": self.rb_remove.isChecked(), "duck": self.sl_duck.value(),
                           "voice": self.sl_voice.value(), "bg": self.sl_bg.value(), "speed": self.sp_speed.value(),
                           "export": self.chk_export.isChecked(), "skip_review": self.chk_skip_review.isChecked()}

    def set_video(self, path: str):
        if Path(path).suffix.lower() not in VIDEO_EXT:
            return
        self.video = path
        self.workdir = workdir_for(path, "")
        self.project = Project.load(path, self.workdir)
        self.drop.set_files([path])
        self._refresh_files()
        self._fill_tables()

    def _refresh_files(self):
        zh, vi = self.find_srt("zh"), os.path.join(self.workdir, f"{Path(self.video).stem}.vi.srt")
        parts = [f"Phụ đề gốc: {'✔ ' + Path(zh).name if zh else '— sẽ nghe giọng tạo mới'}",
                 f"Bản dịch lồng tiếng: {'✔ có sẵn' if os.path.exists(vi) else '— sẽ dịch mới'}",
                 f"Nhạc nền đã tách: {'✔ có sẵn' if os.path.exists(os.path.join(self.workdir, 'instrumental.wav')) else '— sẽ tách'}"]
        if os.path.exists(self.dub_audio_path()):
            parts.append(f"Tiếng lồng: ✔ {Path(self.dub_audio_path()).name}")
        self.lb_files.setText("\n".join(parts))

    def _pick_video(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn video", self.cfg.get("last_dir", ""),
                                           "Video (" + " ".join("*" + x for x in sorted(VIDEO_EXT)) + ")")
        if f:
            self.set_video(f)

    # ================================================================ bảng
    def _voice_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        for label, vid in self.voice_list:
            cb.addItem(label, vid)
        if current and cb.findData(current) < 0:
            cb.addItem(current, current)
        if current:
            cb.setCurrentIndex(cb.findData(current))
        return cb

    def _fill_tables(self):
        proj = self.project
        self.tb_chars.setRowCount(0)
        counts = {}
        for l in proj.lines:
            counts[l.speaker] = counts.get(l.speaker, 0) + 1
        for c in proj.characters:
            r = self.tb_chars.rowCount(); self.tb_chars.insertRow(r)
            self.tb_chars.setItem(r, 0, QTableWidgetItem(f"{c.name}  ({counts.get(c.name, 0)} câu)"))
            g = QComboBox(); g.addItems(["", "nam", "nữ"]); g.setCurrentText(c.gender)
            g.currentTextChanged.connect(lambda v, c=c: (setattr(c, "gender", v), self.project.save()))
            self.tb_chars.setCellWidget(r, 1, g)
            self.tb_chars.setItem(r, 2, QTableWidgetItem(c.desc))
            cb = self._voice_combo(c.voice)
            cb.currentIndexChanged.connect(lambda i, cb=cb, c=c: self._set_voice(c, cb.currentData()))
            self.tb_chars.setCellWidget(r, 3, cb)
            pb = _btn("▶ Nghe thử", "link"); pb.clicked.connect(lambda _=False, c=c: self._preview_voice(c))
            self.tb_chars.setCellWidget(r, 4, pb)
        self.tb_lines.blockSignals(True)
        self.tb_lines.setRowCount(0)
        names = [c.name for c in proj.characters]
        for l in proj.lines:
            r = self.tb_lines.rowCount(); self.tb_lines.insertRow(r)
            it = QTableWidgetItem(str(l.index)); it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tb_lines.setItem(r, 0, it)
            it = QTableWidgetItem(f"{_fmt(l.start)}–{_fmt(l.end)}"); it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.tb_lines.setItem(r, 1, it)
            sc = QComboBox(); sc.addItems(names or [l.speaker]); sc.setCurrentText(l.speaker)
            sc.currentTextChanged.connect(lambda v, l=l: (setattr(l, "speaker", v), self.project.save()))
            self.tb_lines.setCellWidget(r, 2, sc)
            self.tb_lines.setItem(r, 3, QTableWidgetItem(l.text))
            it = QTableWidgetItem(l.src); it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if l.emotion:
                it.setToolTip(f"Cảm xúc: {l.emotion}")
            self.tb_lines.setItem(r, 4, it)
            pb = _btn("▶", "link", "Nghe câu này"); pb.clicked.connect(lambda _=False, l=l: self._play_line(l))
            self.tb_lines.setCellWidget(r, 5, pb)
        self.tb_lines.blockSignals(False)
        if proj.lines:
            self.tabs.setTabText(1, f"Câu thoại ({len(proj.lines)})")
            self.tabs.setTabText(0, f"Nhân vật && giọng ({len(proj.characters)})")

    def _set_voice(self, c: Character, vid):
        c.voice = vid or ""
        self.project.save()

    def _line_edited(self, item: QTableWidgetItem):
        if item.column() != 3:
            return
        idx = int(self.tb_lines.item(item.row(), 0).text())
        l = next((x for x in self.project.lines if x.index == idx), None)
        if l and l.text != item.text():
            l.text = item.text().strip()
            self.project.save()

    # ================================================================ nghe thử
    def _play(self, path: str):
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            return
        if self._player is None:
            self._player = QMediaPlayer(self); self._out = QAudioOutput(self); self._player.setAudioOutput(self._out)
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _ensure_voices(self, then=None):
        if self.voice_list:
            if then:
                then()
            return
        self.lb_engine.setText("VieNeu-TTS: đang nạp model… (lần đầu tải từ Hugging Face)")
        self._vjob = _Job(self.engine.voices)
        self._vjob.result.connect(lambda res, err: self._on_voices(res, err, then))
        self._vjob.start()

    def _on_voices(self, res, err, then):
        if err:
            self.lb_engine.setText(f"✖ Không nạp được VieNeu: {err}")
            return
        self.voice_list = list(res)
        self.lb_engine.setText(f"VieNeu-TTS v3 · {self.engine.device} · {len(self.voice_list)} giọng có sẵn")
        self._fill_tables()
        if then:
            then()

    def _preview_voice(self, c: Character):
        def go():
            text = f"Ta là {c.name}. Hôm nay trời trong gió nhẹ, đúng là ngày lành để lên đường."
            self.lb_stage.setText(f"Đang đọc thử giọng cho {c.name}…")
            self._pjob = _Job(lambda: self._synth_tmp(text, c.voice))
            self._pjob.result.connect(lambda path, err: self._play(path) if path else self.lb_stage.setText(f"✖ {err}"))
            self._pjob.start()
        self._ensure_voices(go)

    def _synth_tmp(self, text: str, voice: str) -> str:
        from .vocal import save_wav
        w = self.engine.synth(text, voice)
        path = os.path.join(tempfile.gettempdir(), f"st_voice_{abs(hash((text, voice)))}.wav")
        save_wav(path, w, self.engine.sr)
        return path

    def _play_line(self, l: Line):
        path = line_cache_path(self.project, l, os.path.join(self.workdir, "tts"))
        if os.path.exists(path):
            self._play(path)
            return
        ch = self.project.char(l.speaker)

        def go():
            self._pjob = _Job(lambda: self._synth_tmp(l.text, ch.voice if ch else ""))
            self._pjob.result.connect(lambda p, err: self._play(p) if p else self.lb_stage.setText(f"✖ {err}"))
            self._pjob.start()
        self._ensure_voices(go)

    # ================================================================ chạy
    def _run(self, mode: str, only: set[int] | None = None):
        if not self.video:
            return QMessageBox.warning(self, "Thiếu video", "Chưa chọn video")
        if mode in ("voice", "export") and not self.project.lines:
            return QMessageBox.information(self, "Chưa có câu thoại", "Bấm “Làm tự động” trước để có câu thoại và nhân vật.")
        if mode == "export" and not os.path.exists(self.dub_audio_path()):
            return QMessageBox.information(self, "Chưa có tiếng lồng", "Bấm “Đọc giọng & trộn” trước.")
        if mode in ("auto",):
            try:
                self.providers()
            except Exception as e:  # noqa: BLE001
                need = not (self.find_srt("zh") and os.path.exists(os.path.join(self.workdir, f"{Path(self.video).stem}.vi.srt")))
                if need:
                    return QMessageBox.warning(self, "Chưa có nhà cung cấp AI",
                                               f"Nghe giọng/dịch/nhận diện nhân vật cần Vertex AI/Gemini ở tab Dịch phụ đề.\n{e}")
        self.store_state(); self.save_cfg()
        self.worker = DubWorker(self, mode, only)
        self.worker.log.connect(self.main_log)
        self.worker.stage.connect(self._on_stage)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self._t0 = time.monotonic(); self._timer.start(500)
        self._running(True)
        self.main_log(f"━━ Lồng tiếng ({mode}): {Path(self.video).name}")
        self.worker.start()

    def _regen(self):
        rows = sorted({i.row() for i in self.tb_lines.selectedIndexes()})
        if not rows:
            return QMessageBox.information(self, "Chưa chọn câu", "Chọn một hoặc vài câu trong bảng Câu thoại.")
        only = {int(self.tb_lines.item(r, 0).text()) for r in rows}
        self._run("voice", only)

    def _on_stage(self, key, text):
        keys = [k for k, _ in STEPS]
        cur = keys.index(key) if key in keys else -1
        for i, k in enumerate(keys):
            lab = self.step_lbls[k]
            old = lab.property("state")
            new = "now" if i == cur else ("done" if i < cur and old in ("now", "done") else (old if i > cur else "skip"))
            lab.setProperty("state", new); self._repolish(lab)
        self.lb_stage.setText(text)
        self.bar.setValue(0); self.lb_pct.setText("0%")

    def _on_progress(self, d, t):
        v = int(d * 100 / t) if t else 0
        self.bar.setValue(v); self.lb_pct.setText(f"{v}%")

    def _tick(self):
        s = int(time.monotonic() - self._t0)
        self.lb_time.setText(f"⏱ {_fmt(s)}")

    def _on_done(self, out, err):
        self._timer.stop()
        self._running(False)
        if self.voice_list == [] and self.engine.tts is not None:
            try:
                self.voice_list = self.engine.voices()
                self.lb_engine.setText(f"VieNeu-TTS v3 · {self.engine.device} · {len(self.voice_list)} giọng có sẵn")
            except Exception:  # noqa: BLE001
                pass
        self.project = Project.load(self.video, self.workdir)
        self._fill_tables()
        self._refresh_files()
        if err:
            self.lb_stage.setText(f"✖ {err}")
            self.main_log(f"  ✖ {err}")
            return
        for lab in self.step_lbls.values():
            if lab.property("state") == "now":
                lab.setProperty("state", "done"); self._repolish(lab)
        if out == "review":
            self.tabs.setCurrentIndex(0)
            self.lb_stage.setText("✔ Đã có nhân vật. Kiểm tra/đổi giọng (bấm ▶ Nghe thử), sửa câu nếu cần, "
                                  "rồi bấm “Đọc giọng & trộn”.")
            return
        self.bar.setValue(100); self.lb_pct.setText("100%")
        self.lb_stage.setText(f"✔ Xong → {out}")

    def _running(self, on: bool):
        for w in (self.btn_auto, self.btn_voice, self.btn_regen, self.btn_export, self.drop):
            w.setEnabled(not on)
        self.btn_stop.setEnabled(on)

    def _open_dir(self):
        d = self.workdir if self.workdir and os.path.isdir(self.workdir) else (str(Path(self.video).parent) if self.video else "")
        if not d:
            return
        if sys.platform == "win32":
            os.startfile(d)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", d])

    def busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def shutdown(self):
        if self.busy():
            self.worker.stop(); self.worker.wait(5000)
        self.store_state()
