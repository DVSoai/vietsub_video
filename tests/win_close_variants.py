import os, sys, shutil
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
mode = sys.argv[1]
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import app.gui as g
from app import theme
d = Path(os.environ.get("TEMP", "/tmp")) / f"cv_{mode}"
shutil.rmtree(d, ignore_errors=True)
g.DATA = d; g.PROMPTS = d / "prompts"; g.CONFIG = d / "settings.json"
a = QApplication(sys.argv); a.setStyle("Fusion"); a.setFont(theme.app_font()); a.setStyleSheet(theme.stylesheet())
a.setWindowIcon(theme.app_icon())
w = g.MainWindow(); w.show()
def step():
    from app.free_keys_dialog import FreeKeysDialog
    dl = FreeKeysDialog(w.cfg, w); dl.show(); QTimer.singleShot(300, dl.accept)
    QTimer.singleShot(900, w.close)
QTimer.singleShot(500, step)
r = a.exec()
import gc
if mode == "delete":            # xoá biến cửa sổ rồi mới shutdown
    w.deleteLater(); a.processEvents(); del w
    a.shutdown()
if mode == "keepref":           # giữ biến cửa sổ khi shutdown (như teardown() bản vừa đẩy)
    w.deleteLater(); a.processEvents()
    a.shutdown()
if mode == "gc":                # xoá biến + gc + xoá app
    w.deleteLater(); a.processEvents(); del w; gc.collect()
    a.shutdown(); del a; gc.collect()
if mode in ("exit", "both"):
    sys.stdout.flush(); sys.stderr.flush(); os._exit(r)
sys.exit(r)
