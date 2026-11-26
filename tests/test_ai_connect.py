"""Kiểm thử nhánh feat/provider-auto-connect: Groq /models, đóng hộp thoại khi đang kiểm tra,
DPAPI (Windows), tab 5 · Quản lý AI, file cấu hình hỏng.

Chạy: python tests/test_ai_connect.py   (Linux: phần DPAPI chỉ kiểm tra là không mã hoá; Windows: mã hoá thật)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NO_PROXY"] = "127.0.0.1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
WIN = sys.platform == "win32"
RES = []
OUT = Path(__file__).parent / "qa_data"
OUT.mkdir(exist_ok=True)


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


SLOW = {"sec": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):   # Groq /models
        if "bad" in self.headers.get("Authorization", ""):
            return self._send(401, {"error": {"message": "Invalid API Key"}})
        self._send(200, {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "openai/gpt-oss-20b"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(SLOW["sec"])
        if "bad" in self.headers.get("Authorization", ""):
            return self._send(401, {"error": {"message": "Invalid API Key"}})
        self._send(200, {"choices": [{"message": {"content": "Xin chào"}}]})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import app.free_keys_dialog as fk  # noqa: E402
import app.gui as g  # noqa: E402
from app import secure_store, theme  # noqa: E402
from app.providers import PROVIDERS  # noqa: E402

_real_get = fk.requests.get
fk.requests.get = lambda url, **kw: _real_get(url.replace("https://api.groq.com/openai/v1", BASE), **kw)
PROVIDERS["NVIDIA NIM (miễn phí)"]["base_url"] = BASE

BOXES = []
QMessageBox.critical = staticmethod(lambda *a, **k: BOXES.append(("critical", a[2] if len(a) > 2 else "")))
QMessageBox.warning = staticmethod(lambda *a, **k: BOXES.append(("warning", a[2] if len(a) > 2 else "")))
QMessageBox.information = staticmethod(lambda *a, **k: BOXES.append(("info", a[2] if len(a) > 2 else "")))

app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


GROQ = "Groq (miễn phí)"; NV = "NVIDIA NIM (miễn phí)"

# ---------------------------------------------------------------- A1 Groq kiểm tra bằng /models
cfg = {}
d = fk.FreeKeysDialog(cfg); d.show()
r = d.rows[GROQ]
r["ed"].setText("gsk_" + "G" * 40); d._check(GROQ)
pump(8, lambda: r["btn"].isEnabled())
ok1 = cfg.get("per_provider", {}).get(GROQ, {}).get("enabled") and r["st"].text().startswith("✔")
r["ed"].setText("gsk_bad" + "x" * 36); d._check(GROQ)
pump(8, lambda: r["btn"].isEnabled())
bad_txt = r["st"].text()
ok2 = bad_txt.startswith("✖") and "gsk_bad" not in cfg[ "per_provider"][GROQ].get("keys", "")
cfg["per_provider"][GROQ]["model"] = "model-khong-ton-tai"
r["ed"].setText("gsk_" + "H" * 40); d._check(GROQ)
pump(8, lambda: r["btn"].isEnabled())
ok3 = "không có" in r["st"].text() and "HHHH" not in cfg["per_provider"][GROQ].get("keys", "")
rec("A1", "Groq: key đúng -> bật; key sai (401) -> không lưu; model không có -> không lưu", ok1 and ok2 and ok3,
    f"đúng={ok1}, sai='{bad_txt}', model lạ='{r['st'].text()}'")

# ---------------------------------------------------------------- A2/A3 đóng hộp thoại khi đang kiểm tra
SLOW["sec"] = 6
d.rows[NV]["ed"].setText("nvapi-" + "n" * 40); d._check(NV)
pump(0.3)
BOXES.clear(); d.reject(); pump(0.2)
still = d.isVisible()
t0 = time.time()
pump(20, lambda: d.rows[NV]["btn"].isEnabled())
wait = time.time() - t0 + 0.5
d.reject(); pump(0.3)
rec("A2", "Đóng hộp thoại khi đang kiểm tra: không đóng, báo chờ; xong thì đóng được",
    still and BOXES and BOXES[0][0] == "info" and not d.isVisible(),
    f"còn mở={still}, thông báo={BOXES[:1]}, phải chờ thêm {wait:.1f}s (máy chủ trả lời sau 6s)")
SLOW["sec"] = 0

# ---------------------------------------------------------------- A4 thông báo lỗi nhà không phải Groq
cfg2 = {}
d2 = fk.FreeKeysDialog(cfg2); d2.show()
d2.rows[NV]["ed"].setText("nvapi-bad" + "z" * 30); d2._check(NV)
pump(8, lambda: d2.rows[NV]["btn"].isEnabled())
msg = d2.rows[NV]["st"].text()
rec("A4", "Key NVIDIA sai: báo lỗi, không lưu (ghi lại nội dung thông báo)", msg.startswith("✖") and
    not cfg2.get("per_provider", {}).get(NV, {}).get("keys"), f"thông báo người dùng thấy: '{msg}'")
d2.reject(); pump(0.2)

# ---------------------------------------------------------------- A5 DPAPI
plain = {"per_provider": {GROQ: {"keys": "gsk_SECRET123\ngsk_SECRET456", "enabled": True, "model": "m"}}}
prot = secure_store.protect_config(plain)
txt = json.dumps(prot)
back = secure_store.restore_config(json.loads(txt))
if WIN:
    rec("A5", "Windows: key lưu dạng keys_dpapi, không còn chữ thường; giải mã lại đúng",
        "SECRET" not in txt and "keys_dpapi" in txt and back["per_provider"][GROQ]["keys"] == plain["per_provider"][GROQ]["keys"]
        and plain["per_provider"][GROQ].get("keys"), f"độ dài keys_dpapi={len(prot['per_provider'][GROQ].get('keys_dpapi',''))}")
else:
    rec("A5", "Không phải Windows: giữ nguyên như cũ (không mã hoá)", back == plain, "linux/mac: không đổi")

# ---------------------------------------------------------------- A6 MainWindow lưu/mở lại
dd = OUT / "ai_connect_cfg"
shutil.rmtree(dd, ignore_errors=True)
g.DATA = dd; g.PROMPTS = dd / "prompts"; g.CONFIG = dd / "settings.json"
w = g.MainWindow(); w.resize(1366, 728); w.show()
w.cfg.setdefault("per_provider", {})[GROQ] = {"keys": "gsk_" + "K" * 40, "enabled": True, "model": "llama-3.3-70b-versatile"}
w._current_provider = None; w._on_provider(w.cb_provider.currentText()); w._save_cfg()
raw = g.CONFIG.read_text(encoding="utf-8")
w.close(); pump(0.3)
w2 = g.MainWindow()
keys2 = w2.cfg.get("per_provider", {}).get(GROQ, {}).get("keys", "")
rec("A6", "Lưu cấu hình rồi mở lại app: key còn nguyên" + (", file không chứa key chữ thường" if WIN else ""),
    keys2.endswith("KKKK") and (("KKKKKKKK" not in raw) if WIN else True),
    f"file có 'keys_dpapi'={'keys_dpapi' in raw}, có key chữ thường={'KKKKKKKK' in raw}")

# ---------------------------------------------------------------- A7 tab 5 · Quản lý AI
w2.resize(1366, 728); w2.show(); w2._go_page(4); pump(0.5)
st = w2.ai_status.text()
w2.grab().save(str(OUT / "tab_quan_ly_ai.png"))
nav_ok = all(b.isVisible() and b.width() > 0 for b in w2.nav_btns)
rec("A7", "Tab 5 · Quản lý AI hiện trạng thái từng nhà; 5 nút tab hiện đủ ở 1366 px",
    "Groq (miễn phí): Đang bật" in st and nav_ok, f"{len(st.splitlines())} dòng, nav rộng={[b.width() for b in w2.nav_btns]}")
w2.close(); pump(0.3)

# ---------------------------------------------------------------- A8 file cấu hình không giải mã được
bad = json.loads(raw)
bad.setdefault("per_provider", {})[GROQ] = {"keys_dpapi": "AAAA", "enabled": True}
g.CONFIG.write_text(json.dumps(bad), encoding="utf-8")
before = g.CONFIG.read_text(encoding="utf-8")
BOXES.clear()
w3 = g.MainWindow(); w3._save_cfg(); w3.close(); pump(0.3)
after = g.CONFIG.read_text(encoding="utf-8")
rec("A8", "keys_dpapi hỏng/không giải mã được: báo lỗi, KHÔNG ghi đè file cấu hình",
    BOXES and BOXES[0][0] == "critical" and before == after, f"thông báo={BOXES[:1]}, file giữ nguyên={before == after}")

print(f"\n{sum(RES)}/{len(RES)} PASS  ({'Windows' if WIN else sys.platform})", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
