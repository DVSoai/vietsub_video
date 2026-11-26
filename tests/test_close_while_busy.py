"""Đóng app khi luồng nền lúc khởi động (đọc danh sách font) còn chạy: tiến trình phải thoát sạch (mã 0).

Chạy: python tests/test_close_while_busy.py child  -> tự chạy app con và báo mã thoát.
"""
import os, subprocess, sys, shutil, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

if len(sys.argv) > 1 and sys.argv[1] == "app":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import app.render_page as rp
    import app.gui as g
    real = rp.font_catalog
    delay = float(sys.argv[2])
    rp.font_catalog = lambda: (time.sleep(delay), real())[1]  # máy chậm: đọc font mất `delay` giây
    d = Path(os.environ.get("TEMP", "/tmp")) / "close_busy"
    shutil.rmtree(d, ignore_errors=True)
    g.DATA = d; g.PROMPTS = d / "prompts"; g.CONFIG = d / "settings.json"
    a = QApplication(sys.argv)
    w = g.MainWindow(); w.show()
    QTimer.singleShot(400, w.close)                            # đóng khi font chưa đọc xong
    code = a.exec()
    print("exec tra ve", code, flush=True)
    g.teardown(a, w, code)
    sys.exit(code)

res = []
for tid, delay, what in (("C1", 3, "chờ luồng xong rồi thoát"), ("C2", 9, "chờ 5 s, luồng chưa xong thì thoát thẳng")):
    t0 = time.time()
    p = subprocess.run([sys.executable, __file__, "app", str(delay)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dt = time.time() - t0
    ok = p.returncode == 0 and "exec tra ve 0" in p.stdout
    res.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} Đóng app khi luồng đọc font còn chạy {delay}s ({what}): "
          f"mã thoát={p.returncode}, {dt:.1f}s" + ("" if ok else f", stderr={p.stderr[-300:]!r}"), flush=True)
print(f"\n{sum(res)}/{len(res)} PASS", flush=True)
sys.exit(0 if all(res) else 1)
