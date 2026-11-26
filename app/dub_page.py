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
    fit_lines, line_cache_path, lines_from_srt, mix, mux_video, scale_lines, speed_bg, synth_lines, tempo,
    workdir_for,
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


def _ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


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
        try:
            provs, prov_err = page.providers(), ""
        except Exception as e:  # noqa: BLE001  (chưa cấu hình AI: chỉ lỗi khi thật sự cần)
            provs, prov_err = [], str(e)
        self.o = {"providers": provs, "prov_err": prov_err, "asr": page.cfg.get("asr", ""),
                  "zh_srt": page.find_srt(page.src_code), "audio": page.dub_audio_path(),
                  "video_out": page.dub_video_path(),
                  "remove": page.rb_remove.isChecked(), "retr": page.chk_retr.isChecked(),
                  "skip_review": page.chk_skip_review.isChecked(), "export": page.chk_export.isChecked(),
                  "mix": page.mix_options(), "hints": page.hints(), "vis": page.visual_settings()}

    def _providers(self):
        if not self.o["providers"]:
            from .providers import ProviderError
            raise ProviderError(self.o["prov_err"] or "Chưa có nhà cung cấp AI")
        return self.o["providers"]

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
        zh = self.o["zh_srt"]
        if zh:
            self.log.emit(f"  Dùng phụ đề gốc có sẵn: {Path(zh).name}")
            return zh
        from .transcribe import Transcriber
        self.stage.emit("asr", "Gemini đang nghe giọng (kèm người nói, giới tính, cảm xúc)…")
        voc = os.path.join(p.workdir, "vocals.wav")
        eng = self.o["asr"]
        tc = Transcriber(self._providers(), p.src_lang, self.o['hints'], threads=6, stop=self.stop_event, engine=eng,
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
        tr = Translator(self._providers(), st, self.stop_event)
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
        old_chars = {c.name: c for c in proj.characters}
        old_lines = {(l.index, l.src): l for l in proj.lines}
        proj.lines = lines_from_srt(vi, zh, spk)
        if not self.o["retr"]:
            # chạy lại không dịch lại: giữ câu đã sửa tay và nhân vật đã chỉnh cho từng câu
            for l in proj.lines:
                o = old_lines.get((l.index, l.src))
                if o:
                    l.text, l.speaker, l.emotion = o.text, o.speaker or l.speaker, o.emotion or l.emotion
        if old_chars and all(l.speaker in old_chars for l in proj.lines) and not self.o["retr"]:
            self.log.emit("  Giữ nguyên nhân vật & giọng đã chọn (không nhận diện lại)")
            proj.characters = list(old_chars.values())
            auto_assign_voices(proj.characters, p.engine.voices())
            proj.save()
            return
        self.stage.emit("chars", "Đang nhận diện nhân vật…")
        try:
            chars, assign = analyze_characters(self._providers()[0], proj.lines, log=self.log.emit, stop=self.stop_event)
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001
            # không có / lỗi AI: chia theo giới tính nghe được (nếu có), còn lại là Người dẫn chuyện
            self.log.emit(f"  ⚠ Không nhận diện nhân vật bằng AI được ({str(e)[:120]}). "
                          "Tạm chia theo giới tính; bạn đổi nhân vật từng câu ở bảng Câu thoại.")
            names = {"nam": "Nhân vật nam", "nữ": "Nhân vật nữ"}
            chars, assign = [], {}
            for l in proj.lines:
                nm = names.get(l.gender_hint, "Người dẫn chuyện")
                assign[l.index] = (nm, l.emotion)
                if nm not in [c.name for c in chars]:
                    chars.append(Character(nm, l.gender_hint if l.gender_hint in ("nam", "nữ") else ""))
        for l in proj.lines:
            sp_, emo = assign.get(l.index, ("", ""))
            l.speaker = sp_ or l.speaker or "Người dẫn chuyện"
            l.emotion = emo or l.emotion
        names = {c.name for c in chars}
        for l in proj.lines:
            if l.speaker not in names:
                g = l.gender_hint if l.gender_hint in ("nam", "nữ") else ""
                chars.append(Character(l.speaker, g)); names.add(l.speaker)
        for c in chars:          # giữ giọng/tốc độ/cao độ/âm lượng người dùng đã chọn cho nhân vật cùng tên
            o = old_chars.get(c.name)
            if o:
                c.voice, c.speed, c.pitch, c.volume = o.voice, o.speed, o.pitch, o.volume
                c.gender = o.gender or c.gender
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
        vs = opt.video_speed
        lines = scale_lines(proj.lines, vs)
        if abs(vs - 1) >= 0.005:
            self.stage.emit("mix", f"Đang đổi tốc độ nhạc nền theo video {vs:g}x…")
            bg = speed_bg(bg, vs, sr)
            self._write_scaled_srt(lines)
        gains = {l.index: (proj.char(l.speaker).volume if proj.char(l.speaker) else 1.0) for l in proj.lines}
        self.stage.emit("mix", "Đang khớp thời gian và trộn với nhạc nền…")
        fitted, starts = fit_lines(lines, wavs, opt.max_speed, sr, log=self.log.emit)
        out = mix(lines, fitted, bg, opt, sr, gains, starts)
        tmp = os.path.join(p.workdir, "dub_mix.wav")
        save_wav(tmp, out, sr)
        encode_m4a(tmp, self.o["audio"])
        os.remove(tmp)
        self.log.emit(f"  ✔ Đã trộn → {self.o['audio']}")

    def _export_visual(self, out: str, vis: dict):
        """Xuất kèm xoá/che chữ Trung, in phụ đề Việt, khung hình, logo, màu (dùng bộ xuất của tab Xuất video)."""
        from .render import Exporter
        p = self.p
        cues = []
        if vis["subs"]:
            cues = [srt.Cue(i + 1, _ts(l.start), _ts(l.end), l.text)
                    for i, l in enumerate(p.project.lines) if l.text.strip()]
        self.stage.emit("mix", "Đang xuất video lồng tiếng (xoá chữ, phụ đề, khung hình, logo)…")
        ex = Exporter(p.video, cues, vis["style"], vis["cover"], vis["opts"], out,
                      progress=lambda pct, sp: self.progress.emit(int(pct), 100), log=self.log.emit, look=vis["look"])
        self._exporter = ex
        res = ex.run()
        if res == "stopped":
            raise Stopped()

    def stop(self):
        self.stop_event.set()
        ex = getattr(self, "_exporter", None)
        if ex:
            ex.stop()

    def _write_scaled_srt(self, lines):
        """Video đổi tốc độ -> phụ đề Việt khớp với video mới để in ở tab Xuất video."""
        p = self.p
        out = str(Path(self.o["video_out"]).with_suffix("")) + ".vi.srt"
        cues = [srt.Cue(i + 1, _ts(l.start), _ts(l.end), l.text) for i, l in enumerate(lines)]
        with open(out, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(srt.compose(cues))
        self.log.emit(f"  ✔ Phụ đề khớp tốc độ mới → {Path(out).name}")

    # ---------------------------------------------------------------- chạy
    def run(self):
        try:
            p = self.p
            os.makedirs(p.workdir, exist_ok=True)
            if self.mode == "auto":
                need_sep = self.o['remove'] or not self.o["zh_srt"]
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
                out = self.o["video_out"]
                vis = self.o["vis"]
                if vis is None:
                    mux_video(p.video, self.o["audio"], out, self.o["mix"].video_speed, stop=self.stop_event)
                else:
                    self._export_visual(out, vis)
                self.log.emit(f"  ✔ Video lồng tiếng: {out}")
                self.done.emit(out, "")
                return
            self.done.emit(self.o["audio"], "")
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
    def __init__(self, cfg: dict, save_cfg, log, providers_getter, out_dir_getter, render_getter=None):
        super().__init__()
        self.render_getter = render_getter or (lambda: None)
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
                          max_speed=self.sp_speed.value(), video_speed=self.sp_vspeed.value())

    def _pick_logo(self):
        f, _ = QFileDialog.getOpenFileName(self, "Chọn logo", "", "Ảnh (*.png *.jpg *.jpeg *.webp)")
        if f:
            self.ed_logo.setText(f)

    def _cover_obj(self):
        from .render import Cover
        mode = ["none", "erase", "blur", "box"][self.cb_cover.currentIndex()]
        return Cover(mode=mode, y_pct=self.sp_cy.value(), h_pct=self.sp_ch.value(), x_pct=self.sp_cx.value(),
                     blur=30, box_color="#000000", box_opacity=95)

    def _align_in_render(self):
        rp = self.render_getter()
        if rp is None or not self.video:
            return
        win = self.window()
        if hasattr(win, "_go_page"):
            win._go_page(3)
        rp.set_video(self.video)
        idx = {"none": 0, "blur": 1, "box": 2, "erase": 3}[self._cover_obj().mode]
        rp.cb_cover.setCurrentIndex(idx)
        rp.sp_cy.setValue(self.sp_cy.value()); rp.sp_ch.setValue(self.sp_ch.value()); rp.sp_cx.setValue(self.sp_cx.value())
        rp._dub_sync = self

    def pull_cover_from_render(self, rp):
        """Sau khi căn ở tab Xuất video, lấy lại vị trí vùng chữ."""
        self.sp_cy.setValue(rp.sp_cy.value()); self.sp_ch.setValue(rp.sp_ch.value()); self.sp_cx.setValue(rp.sp_cx.value())
        m = ["none", "blur", "box", "erase"][rp.cb_cover.currentIndex()]
        self.cb_cover.setCurrentIndex({"none": 0, "erase": 1, "blur": 2, "box": 3}[m])

    def _edit_regions(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        from .render import EditRegion
        dlg = QDialog(self)
        dlg.setWindowTitle("Chỉnh sửa vùng hình ảnh")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Toạ độ % trên video gốc. Chỉ dùng cho nội dung được phép chỉnh sửa."))
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["X %", "Y %", "Rộng %", "Cao %", "Chế độ"])
        layout.addWidget(table)
        def add(region=None):
            region = region or EditRegion()
            row = table.rowCount()
            table.insertRow(row)
            for col, value in enumerate((region.x_pct, region.y_pct, region.w_pct, region.h_pct)):
                spin = QDoubleSpinBox()
                spin.setRange(0 if col < 2 else 1, 100)
                spin.setValue(value)
                table.setCellWidget(row, col, spin)
            combo = QComboBox()
            for label, mode in (("Làm mờ", "blur"), ("Che đen", "box"), ("Nội suy nền", "erase")):
                combo.addItem(label, mode)
            combo.setCurrentIndex(max(0, combo.findData(region.mode)))
            table.setCellWidget(row, 4, combo)
        for region in self.edit_regions:
            add(region)
        buttons = QHBoxLayout()
        plus = QPushButton("Thêm vùng")
        minus = QPushButton("Bỏ vùng đã chọn")
        plus.clicked.connect(lambda: add())
        minus.clicked.connect(lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None)
        buttons.addWidget(plus); buttons.addWidget(minus)
        layout.addLayout(buttons)
        actions = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        actions.accepted.connect(dlg.accept); actions.rejected.connect(dlg.reject)
        layout.addWidget(actions)
        dlg.resize(650, 340)
        if dlg.exec() == QDialog.Accepted:
            self.edit_regions = [EditRegion(*(table.cellWidget(row, col).value() for col in range(4)),
                                            mode=table.cellWidget(row, 4).currentData())
                                 for row in range(table.rowCount())]
            self.btn_regions.setText(f"Chỉnh sửa vùng hình ảnh ({len(self.edit_regions)})…")

    def visual_settings(self):
        """None = chỉ thay tiếng (nhanh, không nén lại hình). Ngược lại: bộ tham số cho Exporter."""
        from dataclasses import replace
        from .render import ExportOpts, Look, SubStyle
        cover = self._cover_obj()
        aspect = self.cb_aspect.currentData() or ""
        logo = self.ed_logo.text().strip()
        rp = self.render_getter()
        base_look = rp._look() if rp is not None else Look()
        color = (base_look.zoom != 100 or base_look.flip or base_look.bright or base_look.contrast != 100
                 or base_look.sat != 100 or base_look.sharpen or bool(base_look.music) or base_look.fade > 0)
        if cover.mode == "none" and not self.chk_subs.isChecked() and not aspect and not logo and not color and not self.edit_regions:
            return None
        look = replace(base_look, speed=self.sp_vspeed.value(), audio_sped=True, aspect=aspect,
                       logo=logo, logo_pos=self.cb_logo_pos.currentData(), voice_vol=100, denoise=False,
                       regions=list(self.edit_regions))
        style = rp._style() if rp is not None else SubStyle()
        opts = rp._opts() if rp is not None else ExportOpts()
        opts = replace(opts, audio_file=self.dub_audio_path(), audio_copy=False)
        return {"cover": cover, "subs": self.chk_subs.isChecked(), "look": look, "style": style, "opts": opts}

    def dub_audio_path(self) -> str:
        """Tiếng lồng theo tốc độ video: 1x nằm cạnh video (tab Xuất video dùng được);
        tốc độ khác có tên riêng trong thư mục dự án để không lẫn khi đổi tốc độ nhiều lần."""
        p = Path(self.video)
        vs = round(self.sp_vspeed.value(), 2)
        if abs(vs - 1) >= 0.005:
            return str(Path(self.workdir) / f"{p.stem}.vi.dub.{vs:g}x.m4a")
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
        self.sp_vspeed = QDoubleSpinBox(); self.sp_vspeed.setRange(0.5, 2.0); self.sp_vspeed.setSingleStep(0.05)
        self.sp_vspeed.setValue(1.0); self.sp_vspeed.setSuffix(" x")
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
        sp_row = QHBoxLayout(); sp_row.setSpacing(12)
        sp_row.addLayout(_field("Tốc độ video", self.sp_vspeed), 1)
        sp_row.addLayout(_field("Đọc nhanh tối đa", self.sp_speed), 1)
        v2.addLayout(sp_row)
        h = QLabel("Tốc độ video: như nút Tốc độ của CapCut, đổi cả hình và nhạc nền, giọng Việt vẫn tự nhiên. "
                   "Đọc nhanh tối đa: câu dài hơn chỗ trống được đọc nhanh lên tới mức này.")
        h.setObjectName("hint"); h.setWordWrap(True); v2.addWidget(h)

        c3, v3 = _card("Tuỳ chọn", "4")
        self.ed_hints = QLineEdit(); self.ed_hints.setPlaceholderText("Tên nhân vật/thuật ngữ, vd: 林澈 = Lâm Triệt; 师尊 = sư tôn")
        self.chk_retr = QCheckBox("Dịch lại kiểu lồng tiếng (kể cả khi đã có .vi.srt)")
        self.chk_skip_review = QCheckBox("Không dừng để kiểm tra giọng (chạy một mạch)")
        self.chk_export = QCheckBox("Xong thì xuất luôn video lồng tiếng (.mp4)")
        self.chk_export.setChecked(True)
        v3.addLayout(_field("Gợi ý tên riêng", self.ed_hints))
        v3.addWidget(self.chk_retr); v3.addWidget(self.chk_skip_review); v3.addWidget(self.chk_export)
        self.lb_engine = QLabel("VieNeu-TTS: chưa nạp"); self.lb_engine.setObjectName("hint"); self.lb_engine.setWordWrap(True)
        v3.addWidget(self.lb_engine)

        c4, v4 = _card("Hình ảnh khi xuất", "3")
        self.cb_cover = QComboBox()
        self.cb_cover.addItems(["Giữ nguyên chữ Trung", "Xoá chữ Trung (nội suy nền)", "Làm mờ chữ Trung", "Che bằng hộp đen"])
        self.sp_cy = QDoubleSpinBox(); self.sp_cy.setRange(0, 99); self.sp_cy.setSuffix(" %"); self.sp_cy.setValue(80)
        self.sp_ch = QDoubleSpinBox(); self.sp_ch.setRange(1, 60); self.sp_ch.setSuffix(" %"); self.sp_ch.setValue(12)
        self.sp_cx = QDoubleSpinBox(); self.sp_cx.setRange(0, 45); self.sp_cx.setSuffix(" %"); self.sp_cx.setValue(5)
        self.chk_subs = QCheckBox("In phụ đề Việt lên video")
        self.cb_aspect = QComboBox()
        for label, val in (("Giữ nguyên khung", ""), ("9:16 · TikTok / Shorts", "9:16"), ("16:9 · YouTube", "16:9"),
                           ("1:1 · vuông", "1:1"), ("4:5 · Facebook", "4:5")):
            self.cb_aspect.addItem(label, val)
        self.ed_logo = QLineEdit(); self.ed_logo.setPlaceholderText("Logo kênh của bạn (.png)")
        self.btn_logo = _btn("Chọn…", "ghost")
        self.cb_logo_pos = QComboBox()
        for label, val in (("Trên phải", "tr"), ("Trên trái", "tl"), ("Dưới phải", "br"), ("Dưới trái", "bl")):
            self.cb_logo_pos.addItem(label, val)
        v4.addLayout(_field("Chữ Trung in sẵn trong video", self.cb_cover))
        self.cover_wrap = QWidget(); cw = QHBoxLayout(self.cover_wrap); cw.setContentsMargins(0, 0, 0, 0); cw.setSpacing(8)
        cw.addLayout(_field("Từ trên xuống", self.sp_cy)); cw.addLayout(_field("Cao", self.sp_ch))
        cw.addLayout(_field("Lề 2 bên", self.sp_cx))
        v4.addWidget(self.cover_wrap)
        self.edit_regions = []
        self.btn_regions = _btn("Chỉnh sửa vùng hình ảnh…", "ghost")
        self.btn_regions.clicked.connect(self._edit_regions)
        v4.addWidget(self.btn_regions)
        v4.addWidget(self.chk_subs)
        lr = QHBoxLayout(); lr.setSpacing(6); lr.addWidget(self.ed_logo, 1); lr.addWidget(self.btn_logo)
        v4.addLayout(_field("Logo kênh của bạn", lr))
        ar = QHBoxLayout(); ar.setSpacing(8)
        ar.addLayout(_field("Khung hình", self.cb_aspect), 3); ar.addLayout(_field("Vị trí logo", self.cb_logo_pos), 2)
        v4.addLayout(ar)
        h4 = QLabel("Kiểu chữ phụ đề, màu sắc, phóng to, lật, nhạc nền thêm lấy theo tab “Xuất video Vietsub”. "
                    "Bấm “Căn ở tab Xuất video” để xem trước vùng xoá chữ.")
        h4.setObjectName("hint"); h4.setWordWrap(True); v4.addWidget(h4)
        self.btn_align = _btn("Căn vùng chữ ở tab Xuất video →", "link"); v4.addWidget(self.btn_align)
        self.cb_cover.currentIndexChanged.connect(lambda i: self.cover_wrap.setVisible(i != 0))
        self.btn_logo.clicked.connect(self._pick_logo)
        self.btn_align.clicked.connect(self._align_in_render)

        from PySide6.QtWidgets import QScrollArea
        inner = QWidget(); left = QVBoxLayout(inner); left.setSpacing(14); left.setContentsMargins(0, 0, 10, 0)
        inner.setMaximumWidth(410)
        left.addWidget(c1); left.addWidget(c2); left.addWidget(c4); left.addWidget(c3); left.addStretch(1)
        lw = QScrollArea(); lw.setWidgetResizable(True); lw.setWidget(inner); lw.setFixedWidth(420)
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
        self.tb_chars = QTableWidget(0, 7); self.tb_chars.setObjectName("jobs")
        self.tb_chars.setHorizontalHeaderLabels(["Nhân vật", "Giới tính", "Giọng VieNeu",
                                                 "Tốc độ", "Cao độ", "Âm lượng", ""])
        self._table_style(self.tb_chars, [QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed,
                                          QHeaderView.Fixed, QHeaderView.Fixed, QHeaderView.Fixed, QHeaderView.Fixed])
        for col, wd in ((1, 82), (2, 168), (3, 82), (4, 70), (5, 92), (6, 36)):
            self.tb_chars.setColumnWidth(col, wd)
        self.tb_chars.verticalHeader().setDefaultSectionSize(48)
        self.tb_chars.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.tb_chars.horizontalHeaderItem(3).setToolTip("Tốc độ đọc riêng của nhân vật (giữ nguyên cao độ)")
        self.tb_chars.horizontalHeaderItem(4).setToolTip("Cao độ, nửa cung: âm = trầm hơn, dương = cao hơn (không đổi tốc độ)")
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
        self.tabs.addTab(self._build_library(), "Thư viện giọng")
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
        self.sp_vspeed.setValue(st.get("vspeed", 1.0))
        self.cb_cover.setCurrentIndex(st.get("cover", 0)); self.cover_wrap.setVisible(st.get("cover", 0) != 0)
        self.sp_cy.setValue(st.get("cy", 80)); self.sp_ch.setValue(st.get("ch", 12)); self.sp_cx.setValue(st.get("cx", 5))
        self.chk_subs.setChecked(st.get("subs", False))
        self.cb_aspect.setCurrentIndex(max(0, self.cb_aspect.findData(st.get("aspect", ""))))
        self.ed_logo.setText(st.get("logo", "")); self.cb_logo_pos.setCurrentIndex(max(0, self.cb_logo_pos.findData(st.get("logo_pos", "tr"))))
        self.chk_export.setChecked(st.get("export", True)); self.chk_skip_review.setChecked(st.get("skip_review", False))

    def store_state(self):
        self.cfg["dub"] = {"remove": self.rb_remove.isChecked(), "duck": self.sl_duck.value(),
                           "voice": self.sl_voice.value(), "bg": self.sl_bg.value(), "speed": self.sp_speed.value(), "vspeed": self.sp_vspeed.value(),
                           "cover": self.cb_cover.currentIndex(), "cy": self.sp_cy.value(), "ch": self.sp_ch.value(),
                           "cx": self.sp_cx.value(), "subs": self.chk_subs.isChecked(),
                           "aspect": self.cb_aspect.currentData() or "", "logo": self.ed_logo.text().strip(),
                           "logo_pos": self.cb_logo_pos.currentData(),
                           "export": self.chk_export.isChecked(), "skip_review": self.chk_skip_review.isChecked()}

    def set_video(self, path: str):
        if Path(path).suffix.lower() not in VIDEO_EXT:
            return
        self.video = path
        self.workdir = workdir_for(path, "")
        self.project = Project.load(path, self.workdir)
        self.edit_regions = []
        self.btn_regions.setText("Chỉnh sửa vùng hình ảnh…")
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
    # ================================================================ thư viện giọng
    def _build_library(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(8)
        top = QHBoxLayout(); top.setSpacing(8)
        self.cb_lib_filter = QComboBox(); self.cb_lib_filter.addItems(["Tất cả giọng", "Giọng nam", "Giọng nữ"])
        self.ed_lib_text = QLineEdit("Sư tôn, đệ tử đến muộn. Xin người trách phạt.")
        self.ed_lib_text.setPlaceholderText("Câu đọc thử")
        self.btn_lib_load = _btn("Nạp danh sách giọng", "ghost")
        top.addWidget(self.cb_lib_filter); top.addWidget(self.ed_lib_text, 1); top.addWidget(self.btn_lib_load)
        v.addLayout(top)
        self.tb_lib = QTableWidget(0, 4); self.tb_lib.setObjectName("jobs")
        self.tb_lib.setHorizontalHeaderLabels(["Giọng VieNeu", "Giới tính", "", "Gán cho nhân vật"])
        self._table_style(self.tb_lib, [QHeaderView.Stretch, QHeaderView.Fixed, QHeaderView.Fixed, QHeaderView.Fixed])
        self.tb_lib.setColumnWidth(1, 80); self.tb_lib.setColumnWidth(2, 90); self.tb_lib.setColumnWidth(3, 190)
        self.tb_lib.verticalHeader().setDefaultSectionSize(44)
        v.addWidget(self.tb_lib, 1)
        self.lb_lib = QLabel("Danh sách giọng có sẵn của VieNeu-TTS (tải từ Hugging Face lần đầu). "
                             "Bấm ▶ để nghe từng giọng với câu ở trên, rồi gán cho nhân vật.")
        self.lb_lib.setObjectName("hint"); self.lb_lib.setWordWrap(True); v.addWidget(self.lb_lib)
        self.btn_lib_load.clicked.connect(lambda: self._ensure_voices())
        self.cb_lib_filter.currentIndexChanged.connect(lambda _: self._fill_library())
        self.tabs.currentChanged.connect(lambda i: self._ensure_voices() if i == 2 and not self.voice_list else None)
        return w

    def _fill_library(self):
        from .dub import voice_gender
        if not hasattr(self, "tb_lib"):
            return
        want = {0: None, 1: "nam", 2: "nữ"}[self.cb_lib_filter.currentIndex()]
        self.tb_lib.setRowCount(0)
        for label, vid in self.voice_list:
            g = voice_gender(label)
            if want and g != want:
                continue
            r = self.tb_lib.rowCount(); self.tb_lib.insertRow(r)
            self.tb_lib.setItem(r, 0, QTableWidgetItem(label))
            self.tb_lib.setItem(r, 1, QTableWidgetItem(g or "?"))
            pb = _btn("▶ Nghe", "link"); pb.clicked.connect(lambda _=False, vid=vid: self._preview_lib(vid))
            self.tb_lib.setCellWidget(r, 2, pb)
            cb = QComboBox(); cb.setStyleSheet(self._cell_css)
            cb.addItem("— chọn nhân vật —", "")
            for c in self.project.characters:
                cb.addItem(c.name + (" ✓" if c.voice == vid else ""), c.name)
            cb.activated.connect(lambda i, cb=cb, vid=vid: self._assign_from_lib(cb.itemData(i), vid))
            self.tb_lib.setCellWidget(r, 3, cb)
        self.tabs.setTabText(2, f"Thư viện giọng ({len(self.voice_list)})")

    def _preview_lib(self, vid: str):
        text = self.ed_lib_text.text().strip() or "Xin chào, đây là giọng đọc thử."
        self.lb_lib.setText("Đang đọc thử…")
        self._pjob = _Job(lambda: self._synth_tmp(text, vid))
        self._pjob.result.connect(lambda p, err: (self._play(p), self.lb_lib.setText("▶ Đang phát")) if p
                                  else self.lb_lib.setText(f"✖ {err}"))
        self._pjob.start()

    def _assign_from_lib(self, name, vid):
        c = self.project.char(name) if name else None
        if not c:
            return
        c.voice = vid
        self.project.save()
        self._fill_tables()
        self.lb_lib.setText(f"✔ Đã gán giọng cho {name}")

    def _voice_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        if not self.voice_list:
            cb.addItem("Bấm để nạp giọng…", current or "")
            cb.setToolTip("Danh sách giọng VieNeu chưa nạp. Mở cái này hoặc tab Thư viện giọng để nạp.")
            cb.activated.connect(lambda _: self._ensure_voices())
            return cb
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
            it = QTableWidgetItem(f"{c.name}  ({counts.get(c.name, 0)} câu)" + (f"\n{c.desc}" if c.desc else ""))
            it.setToolTip(c.desc)
            self.tb_chars.setItem(r, 0, it)
            g = QComboBox(); g.addItems(["?", "nam", "nữ"]); g.setCurrentText(c.gender or "?")
            g.setStyleSheet(self._cell_css)
            g.currentTextChanged.connect(lambda v, c=c: (setattr(c, "gender", "" if v == "?" else v), self.project.save()))
            self.tb_chars.setCellWidget(r, 1, g)
            cb = self._voice_combo(c.voice); cb.setStyleSheet(self._cell_css)
            cb.currentIndexChanged.connect(lambda i, cb=cb, c=c: self._set_voice(c, cb.currentData()))
            self.tb_chars.setCellWidget(r, 2, cb)
            self.tb_chars.setCellWidget(r, 3, self._spin(c, "speed", 0.7, 1.5, 0.05, "x", 2))
            self.tb_chars.setCellWidget(r, 4, self._spin(c, "pitch", -8, 8, 1, "", 0))
            self.tb_chars.setCellWidget(r, 5, self._spin(c, "volume", 0.3, 2.0, 0.1, "", 1, pct=True))
            pb = _btn("▶", "link", f"Nghe thử giọng {c.name}"); pb.clicked.connect(lambda _=False, c=c: self._preview_voice(c))
            self.tb_chars.setCellWidget(r, 6, pb)
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
        self._fill_library()
        if proj.lines:
            self.tabs.setTabText(1, f"Câu thoại ({len(proj.lines)})")
            self.tabs.setTabText(0, f"Nhân vật && giọng ({len(proj.characters)})")

    _cell_css = "QComboBox { padding: 3px 16px 3px 6px; } QComboBox::drop-down { width: 16px; }"

    def _spin(self, c: Character, attr: str, lo, hi, step, suffix, dec, pct=False):
        """Ô chọn nhanh như các nấc của CapCut (tốc độ / cao độ / âm lượng)."""
        cb = QComboBox(); cb.setStyleSheet(self._cell_css)
        vals, v = [], lo
        while v <= hi + 1e-9:
            vals.append(round(v, 2)); v += step
        cur = round(getattr(c, attr), 2)
        if cur not in vals:
            vals.append(cur); vals.sort()
        for x in vals:
            if pct:
                label = f"{int(round(x * 100))}%"
            elif attr == "pitch":
                label = "0" if x == 0 else f"{x:+.0f}"
            else:
                label = f"{x:.2f}".rstrip("0").rstrip(".") + suffix
            cb.addItem(label, x)
        cb.setCurrentIndex(vals.index(cur))
        cb.currentIndexChanged.connect(lambda _: (setattr(c, attr, float(cb.currentData())), self.project.save()))
        return cb

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
        if getattr(self, "_vjob", None) and self._vjob.isRunning():
            return
        self.lb_engine.setText("VieNeu-TTS: đang nạp model… (lần đầu tải từ Hugging Face)")
        if hasattr(self, "lb_lib"):
            self.lb_lib.setText("Đang nạp VieNeu-TTS và danh sách giọng… lần đầu sẽ tải model từ Hugging Face.")
        self._vjob = _Job(self.engine.voices)
        self._vjob.result.connect(lambda res, err: self._on_voices(res, err, then))
        self._vjob.start()

    def _on_voices(self, res, err, then):
        if err:
            self.lb_engine.setText(f"✖ Không nạp được VieNeu: {err}")
            return
        self.voice_list = list(res)
        self.lb_engine.setText(f"VieNeu-TTS v3 · {self.engine.device} · {len(self.voice_list)} giọng có sẵn")
        if self.project.characters and not any(c.voice for c in self.project.characters):
            auto_assign_voices(self.project.characters, self.voice_list); self.project.save()
        self._fill_tables()
        if then:
            then()

    def _preview_voice(self, c: Character):
        def go():
            text = f"Ta là {c.name}. Hôm nay trời trong gió nhẹ, đúng là ngày lành để lên đường."
            self.lb_stage.setText(f"Đang đọc thử giọng cho {c.name}…")
            self._pjob = _Job(lambda: self._synth_tmp(text, c.voice, c.speed, c.pitch))
            self._pjob.result.connect(lambda path, err: self._play(path) if path else self.lb_stage.setText(f"✖ {err}"))
            self._pjob.start()
        self._ensure_voices(go)

    def _synth_tmp(self, text: str, voice: str, speed: float = 1.0, pitch: float = 0.0) -> str:
        from .vocal import save_wav
        w = tempo(self.engine.synth(text, voice), speed, self.engine.sr, pitch)
        path = os.path.join(tempfile.gettempdir(), f"st_voice_{abs(hash((text, voice, speed, pitch)))}.wav")
        save_wav(path, w, self.engine.sr)
        return path

    def _play_line(self, l: Line):
        ch = self.project.char(l.speaker)

        def go():
            self._pjob = _Job(lambda: self._synth_tmp(l.text, ch.voice if ch else "",
                                                      ch.speed if ch else 1.0, ch.pitch if ch else 0.0))
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
            return QMessageBox.information(self, "Chưa có tiếng lồng",
                                           f"Chưa có tiếng lồng ở tốc độ {self.sp_vspeed.value():g}x. "
                                           "Bấm “Đọc giọng & trộn” trước.")
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
