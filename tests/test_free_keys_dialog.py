"""Kiểm thử hộp thoại “Kết nối AI miễn phí”: tự nhận key từ clipboard, kiểm tra, bật, lưu; tab Dịch dùng ngay.

Chạy: python tests/test_free_keys_dialog.py  (máy chủ giả lập thay cho Groq/NVIDIA thật)
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

RES = []


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        auth = self.headers.get("Authorization", "")
        SEEN.append((self.path, auth[-6:], body.get("model")))
        if "bad" in auth:
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":{"message":"Invalid API Key"}}')
            return
        resp = json.dumps({"choices": [{"message": {"content": "Xin chào"}}]})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(resp.encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.gui as g  # noqa: E402
from app import theme  # noqa: E402
from app.free_keys_dialog import FreeKeysDialog, guess_provider  # noqa: E402
from app.providers import PROVIDERS  # noqa: E402

for n in ("Groq (miễn phí)", "NVIDIA NIM (miễn phí)"):
    PROVIDERS[n]["base_url"] = BASE + "/" + n.split()[0].lower()

d = Path(__file__).parent / "qa_data" / "free_keys_cfg"
shutil.rmtree(d, ignore_errors=True)
g.DATA = d; g.PROMPTS = d / "prompts"; g.CONFIG = d / "settings.json"
app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())

rec("K1", "Đoán nhà cung cấp theo tiền tố key",
    guess_provider("gsk_" + "a" * 40) == "Groq (miễn phí)" and guess_provider("nvapi-" + "b" * 40) == "NVIDIA NIM (miễn phí)"
    and guess_provider("csk-" + "c" * 30) == "Cerebras (miễn phí)" and guess_provider("hello") == "",
    "gsk_→Groq, nvapi-→NVIDIA, csk-→Cerebras, chữ thường→không")

w = g.MainWindow(); w.show(); w._go_page(1)
res = {}


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


def drive():
    dlg = [x for x in app.topLevelWidgets() if isinstance(x, FreeKeysDialog) and x.isVisible()][0]
    groq = "gsk_" + "Q" * 40
    res["default_off"] = not dlg.chk_watch.isChecked()
    QGuiApplication.clipboard().setText("gsk_" + "Z" * 40)      # chưa tích ô -> không được đọc clipboard
    pump(2)
    res["ignored"] = dlg.rows["Groq (miễn phí)"]["ed"].text() == ""
    dlg.chk_watch.setChecked(True)                              # người dùng tự bật
    QGuiApplication.clipboard().setText(groq)                   # như bấm Copy trên trang Groq
    res["auto"] = pump(8, lambda: dlg.rows["Groq (miễn phí)"]["st"].text().startswith("✔"))
    dlg.rows["NVIDIA NIM (miễn phí)"]["ed"].setText("nvapi-bad" + "x" * 30)   # key sai
    dlg._check("NVIDIA NIM (miễn phí)")
    pump(6, lambda: dlg.rows["NVIDIA NIM (miễn phí)"]["st"].text().startswith("✖"))
    res["bad"] = dlg.rows["NVIDIA NIM (miễn phí)"]["st"].text()
    dlg.rows["NVIDIA NIM (miễn phí)"]["ed"].setText("nvapi-good" + "y" * 30)
    dlg._check("NVIDIA NIM (miễn phí)")
    res["good"] = pump(6, lambda: dlg.rows["NVIDIA NIM (miễn phí)"]["st"].text().startswith("✔"))
    dlg.grab().save(str(Path(__file__).parent / "qa_data" / "ket_noi_ai_mien_phi.png"))
    dlg.accept()


from PySide6.QtCore import QTimer  # noqa: E402
QTimer.singleShot(500, drive)
w.open_free_keys()
pp = w.cfg.get("per_provider", {})
rec("K2a", "Mặc định KHÔNG đọc clipboard; chưa tích ô thì bỏ qua key vừa copy",
    res.get("default_off") and res.get("ignored") and "gsk_ZZZZ" not in pp.get("Groq (miễn phí)", {}).get("keys", ""),
    f"ô tắt sẵn={res.get('default_off')}, ô key Groq vẫn trống={res.get('ignored')}")
rec("K2", "Tích ô rồi bấm Copy key Groq trên trang -> app tự nhận, kiểm tra, bật", res.get("auto")
    and pp.get("Groq (miễn phí)", {}).get("enabled") and "gsk_" in pp.get("Groq (miễn phí)", {}).get("keys", ""),
    f"Groq={pp.get('Groq (miễn phí)')}")
rec("K3", "Key sai báo lỗi rõ, không bật", res.get("bad", "").startswith("✖") and "nvapi-bad" not in
    pp.get("NVIDIA NIM (miễn phí)", {}).get("keys", ""), res.get("bad"))
rec("K4", "Key đúng NVIDIA được bật", res.get("good") and pp.get("NVIDIA NIM (miễn phí)", {}).get("enabled"),
    pp.get("NVIDIA NIM (miễn phí)"))
provs = w._make_translate_providers()
labels = [p.label for p in provs]
rec("K5", "Tab Dịch dùng ngay cả 2 nhà, không phải nhập lại", any("Groq" in l for l in labels)
    and any("NVIDIA" in l for l in labels), labels)
w.close()
saved = json.loads((d / "settings.json").read_text(encoding="utf-8")).get("per_provider", {})
rec("K6", "Đã lưu vào settings.json (mở lại app vẫn còn)", saved.get("Groq (miễn phí)", {}).get("enabled") and
    saved.get("NVIDIA NIM (miễn phí)", {}).get("enabled"), list(k for k, v in saved.items() if v.get("enabled")))
print(f"\n{sum(RES)}/{len(RES)} PASS")
os._exit(0 if all(RES) else 1)
