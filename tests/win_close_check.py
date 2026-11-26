"""Mở app như người dùng, (tuỳ chọn) mở/đóng hộp thoại AI miễn phí, rồi đóng cửa sổ bằng app.exec() bình thường."""
import os, sys, shutil
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
root = Path(sys.argv[1]).resolve(); mode = sys.argv[2]
sys.path.insert(0, str(root))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import app.gui as g
d = Path(os.environ.get("TEMP", "/tmp")) / f"closecheck_{mode}"
shutil.rmtree(d, ignore_errors=True)
g.DATA = d; g.PROMPTS = d / "prompts"; g.CONFIG = d / "settings.json"
a = QApplication(sys.argv)
w = g.MainWindow(); w.show()
def step():
    if mode == "dialog":
        from app.free_keys_dialog import FreeKeysDialog
        dl = FreeKeysDialog(w.cfg, w); dl.show(); QTimer.singleShot(300, dl.accept)
    w.cfg.setdefault("per_provider", {})["Groq (miễn phí)"] = {"keys": "gsk_" + "T" * 40, "enabled": True}
    w._save_cfg()
    QTimer.singleShot(800, w.close)
QTimer.singleShot(500, step)
r = a.exec()
print("exec tra ve", r, flush=True)
g.teardown(a, w); del w      # đúng như app.gui.main()
sys.exit(r)
