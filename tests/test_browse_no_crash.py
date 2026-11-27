"""Tab Tìm & tải phim: đổi kiểu/thể loại liên tục khi đang bật 'Dịch tiêu đề' không được làm app tự tắt.

Lỗi người dùng gặp (26/11/2026): chọn Bảng xếp hạng rồi đổi mục -> app tự đóng. Nguyên nhân: việc dịch tiêu đề
(QThread) còn chạy thì bị thay bằng việc mới -> Qt huỷ luồng đang chạy -> tiến trình bị dừng cưỡng bức.
Bài kiểm thử chạy app trong tiến trình con để bắt được trường hợp app bị tắt.
Chạy: python tests/test_browse_no_crash.py
"""
import os, subprocess, sys, time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "child":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["NO_PROXY"] = "127.0.0.1"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import test_bilibili_browse_server as srvmod  # máy chủ Bilibili giả lập
    from PySide6.QtWidgets import QApplication
    import app.bilibili as bl
    from app.search_page import SearchPage

    class SlowProv:                     # AI dịch tiêu đề chậm (như mạng thật)
        label = "giả lập"; name = "giả lập"
        def chat(self, system, user, **kw):
            time.sleep(1.5)
            import json
            obj = json.loads(user[user.find("{"):user.rfind("}") + 1])        # {"id": "câu"} -> {"id": "bản dịch"}
            return json.dumps({k: f"Tiêu đề {k}" for k in obj}, ensure_ascii=False)

    a = QApplication(sys.argv)
    cfg = {"search": {"translate_titles": True, "auto_browse": True}}
    page = SearchPage(cfg, lambda: None, lambda m: None, lambda: [SlowProv()], bili=bl.Bili(api=srvmod.BASE, web=srvmod.BASE))
    page.show()

    def pump(sec, until=None):
        t0 = time.time()
        while time.time() - t0 < sec:
            a.processEvents(); time.sleep(0.01)
            if until and until():
                return True
        return False
    pump(1.5, lambda: len(page.cards) == 20)
    # người dùng bấm liên tục: đổi kiểu, đổi mục, lật trang — trong lúc đang dịch tiêu đề
    page.cb_bmode.setCurrentText("🏆 Bảng xếp hạng"); pump(0.4)
    page.cb_bcat.setCurrentText("Phim ảnh · review, cắt phim (影视)"); pump(0.4)
    page.cb_bcat.setCurrentText("Tiểu kịch trường · phim ngắn tự diễn (小剧场)"); pump(0.3)
    page.btn_next.click(); pump(0.3)
    page.cb_bmode.setCurrentText("✨ Đề xuất trong mục"); pump(0.3)
    page.cb_bcat.setCurrentText("Ẩm thực (美食)")
    ok = pump(15, lambda: page.source == ("browse", "rcmd", 1020) and page.cards
              and page.cards[0].v.bvid.startswith("BV1C1020"))
    print("KET_QUA", ok, page.source, page.cards[0].v.bvid if page.cards else None, flush=True)
    # thoát giống app thật (MainWindow.closeEvent): dừng luồng tải, chờ việc nền xong
    page.shutdown()
    for j in [page._tr_job, page._search_job] + list(getattr(page, "_old_jobs", [])):
        if j is not None:
            j.wait(10000)
    page.close(); a.processEvents()
    sys.exit(0)

t0 = time.time()
p = subprocess.run([sys.executable, __file__, "child"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                   cwd=str(Path(__file__).parent), timeout=120)
line = next((l for l in p.stdout.splitlines() if l.startswith("KET_QUA")), "")
ok = p.returncode == 0 and line.startswith("KET_QUA True")
crash = "Destroyed while thread" in p.stderr
print(f"[{'PASS' if ok else 'FAIL'}] N1 Đổi kiểu/thể loại/lật trang liên tục khi đang dịch tiêu đề: app không tự tắt, "
      f"hiện đúng lựa chọn cuối: mã thoát={p.returncode}, {line or 'không chạy hết'}"
      + (", Qt báo 'QThread: Destroyed while thread is still running'" if crash else ""), flush=True)
print(f"\n{int(ok)}/1 PASS", flush=True)
sys.exit(0 if ok else 1)
