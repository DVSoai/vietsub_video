"""Khung 'Hàng chờ tải' (tab Tìm & tải phim) trong cửa sổ app 1366×768.

Người dùng gửi ảnh (26/11/2026): 8 phim (1 đang tải, 7 chờ) mà khung chỉ hiện được 1–2 phim; muốn nút
Xóa / Thử lại cho phim đã dừng và xem được hết các phim. Việc tải thật được thay bằng tải giả (chạy dần
tới 100%, dừng / tạm dừng / bỏ được như Downloader thật) để kiểm thử nhanh, không cần mạng.
Chạy: python tests/run_check.py tests/test_download_queue.py
"""
import json, os, shutil, sys, time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
RES = []
OUT = Path(__file__).parent / "qa_data"
OUT.mkdir(exist_ok=True)


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


from PySide6.QtWidgets import QApplication  # noqa: E402
import app.bilibili as bl  # noqa: E402
import app.gui as g  # noqa: E402
from app import theme  # noqa: E402

RAN = []


def fake_run(self, job):                       # như Downloader.run: kiểm tra _halt(), ghi trạng thái bằng _stopped()
    RAN.append(job.title)
    job.state, job.info = "running", "Đang tải hình"
    self.on_update(job)
    while job.pct < 100:
        if self._halt(job):
            self._stopped(job); self.on_update(job); return
        time.sleep(0.05)
        job.pct = min(100.0, job.pct + 2.5)
        job.info = f"Đang tải hình {job.pct * 7.65:.1f}/765.3 MB · 0.35 MB/s"
        self.on_update(job)
    job.state, job.file, job.info = "done", os.path.join(job.out_dir, job.title + ".mp4"), ""
    self.on_update(job)


bl.Downloader.run = fake_run

dd = OUT / "queue_cfg"
shutil.rmtree(dd, ignore_errors=True); dd.mkdir(parents=True)
g.DATA = dd; g.PROMPTS = dd / "prompts"; g.CONFIG = dd / "settings.json"
g.CONFIG.write_text(json.dumps({"search": {"auto_browse": False, "out_dir": str(dd / "phim")}}), encoding="utf-8")
app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


w = g.MainWindow(); w.resize(1366, 728); w.show(); w._go_page(0); pump(0.5)
page = w.search_page
names = ["沉睡万年，逆徒皆成大帝第一季1"] + [f"沉睡万年 第{i}集" for i in range(2, 9)]
for n in names:
    page._enqueue(bl.DownloadJob(f"https://www.bilibili.com/video/BV1x{len(RAN)}", n, str(dd / "phim")))
pump(4, lambda: any(r.job.state == "running" for r in page.rows.values()))
pump(0.6)
rows = list(page.rows.values())
sb = page.q_scroll.verticalScrollBar().value()
vp = page.q_scroll.viewport().rect()
fully = [r for r in rows if vp.contains(r.geometry().translated(0, -sb))]
rec("Q1", "Cửa sổ 1366×768, 8 phim (1 đang tải, 7 chờ): nhìn thấy trọn cả 8 phim, không bị khuất",
    len(fully) == 8, f"thấy trọn {len(fully)}/8; khung danh sách cao {page.q_scroll.height()}px; "
    f"1 phim chờ cao {rows[3].height()}px, phim đang tải cao {rows[0].height()}px")
w.grab().save(str(OUT / "hang_cho_tai_8_phim.png"))

page.dthread.stop_all()                                          # như bấm ■ Dừng hết cũ / tắt giữa chừng
pump(4, lambda: all(r.job.state == "stopped" for r in rows)); pump(0.3)
r0, r3 = rows[0], rows[3]
rec("Q2", "Phim đã dừng có đủ 2 nút: “Thử lại” và “Xóa”",
    all(r.btn_resume.isVisible() and r.btn_resume.text() == "Thử lại" and r.btn_remove.isVisible()
        and r.btn_remove.text() == "Xóa" for r in rows) and not r3.lb_info.isVisible(),
    f"phim 1: [{r0.btn_resume.text()}] [{r0.btn_remove.text()}], phim 4: [{r3.btn_resume.text()}] [{r3.btn_remove.text()}], "
    f"dòng chi tiết ẩn ở phim đã dừng={not r3.lb_info.isVisible()}")
w.grab().save(str(OUT / "hang_cho_tai_da_dung.png"))
from PySide6.QtCore import QPoint  # noqa: E402
vp_g = page.q_scroll.viewport()
def inside(btn):
    tl = btn.mapTo(vp_g, QPoint(0, 0)); br = btn.mapTo(vp_g, QPoint(btn.width() - 1, btn.height() - 1))
    return vp_g.rect().contains(tl) and vp_g.rect().contains(br) or not vp_g.rect().intersects(
        btn.geometry().translated(btn.parentWidget().mapTo(vp_g, QPoint(0, 0))))
side_right = page.q_scroll.mapTo(w, QPoint(page.q_scroll.width(), 0)).x()
cut = [(r.job.title, b.text()) for r in rows for b in (r.btn_resume, r.btn_remove) if b.isVisible() and
       b.mapTo(vp_g, QPoint(b.width(), 0)).x() > vp_g.width()]
combo_cut = [c.currentText() for c in (page.cb_order, page.cb_dur)
             if c.fontMetrics().horizontalAdvance(c.currentText()) + 44 > c.width()]   # chữ + lề + mũi tên
rec("Q2b", "Không gì bị khuất: nút Thử lại / Xóa nằm trọn trong khung; cột Hàng chờ không tràn ra ngoài cửa sổ; ô lọc không cắt chữ",
    not cut and side_right <= w.width() and not combo_cut,
    f"nút bị khuất={cut}, mép phải cột={side_right}/{w.width()}px, ô lọc bị cắt={combo_cut}")

part_before = r0.job.pct
r0.btn_resume.click()
pump(8, lambda: r0.job.state == "done"); pump(0.3)
rec("Q3", "Bấm “Thử lại” ở phim đã dừng: tải tiếp từ phần đã có tới xong",
    r0.job.state == "done" and part_before > 0 and RAN.count(names[0]) == 2,
    f"đã có {part_before:.0f}% → {r0.job.state}, số lần chạy={RAN.count(names[0])}")

r3.btn_remove.click(); pump(0.5)
rec("Q4", "Bấm “Xóa” ở phim đã dừng: phim biến khỏi danh sách, còn 7 phim",
    id(r3.job) not in page.rows and len(page.rows) == 7 and "(7)" in page.btn_all.text(),
    f"còn {len(page.rows)} phim, nút='{page.btn_all.text()}'")

page._show_all(); pump(0.4)
dlg = page._all_dlg
states = [dlg.tb.item(i, 2).text() for i in range(dlg.tb.rowCount())]
dlg.grab().save(str(OUT / "hang_cho_tai_xem_tat_ca.png"))
dlg.tb.selectRow(1); dlg.tb.setRangeSelected(dlg.tb.visualItemRect(dlg.tb.item(1, 0)) and
                                          __import__("PySide6.QtWidgets", fromlist=["QTableWidgetSelectionRange"])
                                          .QTableWidgetSelectionRange(1, 0, 2, 0), True)
sel = [j.title for j in dlg._selected()]
for b in dlg.findChildren(type(page.btn_all)):
    if b.text() == "Tiếp tục / Thử lại":
        b.click()
pump(12, lambda: all(r.job.state == "done" for r in page.rows.values() if r.job.title in sel)); pump(0.3)
states2 = [dlg.tb.item(i, 2).text() for i in range(dlg.tb.rowCount())]
rec("Q5", "Bảng “Xem tất cả” liệt kê đủ phim + trạng thái; chọn 2 phim rồi “Tiếp tục / Thử lại” -> cả 2 tải xong, bảng tự cập nhật",
    dlg.tb.rowCount() == 7 and states[0] == "Xong" and states[1] == "Đã dừng" and len(sel) == 2
    and states2[1] == "Xong" and states2[2] == "Xong",
    f"trước: {states[:4]}…; đã chọn {sel}; sau: {states2[:4]}…")

dlg.close(); w.close(); pump(0.3)
print(f"\n{sum(RES)}/{len(RES)} PASS", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
