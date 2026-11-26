"""Model bị nhà cung cấp tắt (Groq llama-3.3-70b-versatile, llama-3.1-8b-instant… từ 16/8/2026).

Máy chủ giả lập giống Groq thật: /models vẫn liệt kê Llama, nhưng gọi Llama thì trả
HTTP 404 "The model `…` does not exist or you do not have access to it."
Chạy: python tests/test_retired_models.py
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


GONE = {"llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3.6-27b"}
CALLS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        CALLS.append(("GET", self.path))
        if "bad" in self.headers.get("Authorization", ""):
            return self._send(401, {"error": {"message": "Invalid API Key"}})
        ids = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
               "whisper-large-v3"]
        self._send(200, {"data": [{"id": i} for i in ids]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        m = body.get("model")
        CALLS.append(("POST", m))
        if m in GONE or m == "model-tu-go":
            return self._send(404, {"error": {"message": f"The model `{m}` does not exist or you do not have access to it.",
                                              "type": "invalid_request_error", "code": "model_not_found"}})
        self._send(200, {"choices": [{"message": {"content": "Xin chào"}}]})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import app.free_keys_dialog as fk  # noqa: E402
import app.gui as g  # noqa: E402
from app import theme  # noqa: E402
from app.providers import PROVIDERS, make_provider, model_gone  # noqa: E402

GROQ = "Groq (miễn phí)"
PROVIDERS[GROQ]["base_url"] = BASE
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


# R1 đúng tình huống của người dùng: cấu hình cũ còn llama-3.3-70b-versatile
cfg = {"per_provider": {GROQ: {"model": "llama-3.3-70b-versatile"}}}
d = fk.FreeKeysDialog(cfg); d.show()
r = d.rows[GROQ]
r["ed"].setText("gsk_" + "A" * 40); CALLS.clear(); d._check(GROQ)
pump(10, lambda: r["btn"].isEnabled())
pc = cfg["per_provider"][GROQ]
posted = [c[1] for c in CALLS if c[0] == "POST"]
rec("R1", "Cấu hình cũ chọn llama-3.3-70b-versatile: kiểm tra vẫn thành công, tự dùng openai/gpt-oss-120b",
    r["st"].text().startswith("✔") and pc.get("model") == "openai/gpt-oss-120b" and pc.get("enabled")
    and "llama-3.3-70b-versatile" not in posted, f"trạng thái='{r['st'].text()}', đã gọi={posted}, model lưu={pc.get('model')}")

# R2 app chưa biết model đã tắt (Groq vẫn liệt kê trong /models) -> gọi thử bị 404 -> tự thử model kế tiếp
import app.providers as pv  # noqa: E402
ret0 = dict(pv.RETIRED[GROQ]); pv.RETIRED[GROQ].clear()
cfg2 = {"per_provider": {GROQ: {"model": "llama-3.3-70b-versatile"}}}
d2 = fk.FreeKeysDialog(cfg2); d2.show()
d2.rows[GROQ]["ed"].setText("gsk_" + "B" * 40); CALLS.clear(); d2._check(GROQ)
pump(10, lambda: d2.rows[GROQ]["btn"].isEnabled())
posted2 = [c[1] for c in CALLS if c[0] == "POST"]
pv.RETIRED[GROQ].update(ret0)
rec("R2", "Model còn trong danh sách nhưng gọi bị 404 model_not_found: tự thử model kế tiếp, lưu model chạy được",
    posted2[:2] == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
    and cfg2["per_provider"][GROQ].get("model") == "openai/gpt-oss-120b" and d2.rows[GROQ]["st"].text().startswith("✔"),
    f"đã gọi={posted2}, trạng thái='{d2.rows[GROQ]['st'].text()}'")

# R3 tất cả model đều đã tắt -> báo rõ, không bật
saved = PROVIDERS[GROQ]["models"]
PROVIDERS[GROQ]["models"] = ["llama-3.1-8b-instant"]
cfg3 = {"per_provider": {GROQ: {"model": "llama-3.3-70b-versatile"}}}
ret = dict(pv.RETIRED[GROQ]); pv.RETIRED[GROQ].clear()       # giả sử app chưa biết model nào đã tắt
d3 = fk.FreeKeysDialog(cfg3); d3.show()
d3.rows[GROQ]["ed"].setText("gsk_" + "C" * 40); d3._check(GROQ)
pump(10, lambda: d3.rows[GROQ]["btn"].isEnabled())
st3 = d3.rows[GROQ]["st"].text()
rec("R3", "Không model nào dùng được: báo 'Key đúng nhưng không model nào dùng được: HTTP 404 …', không bật",
    st3.startswith("✖ Key đúng nhưng không model nào") and "HTTP 404" in st3 and not cfg3["per_provider"][GROQ].get("enabled"),
    f"'{st3}'")
pv.RETIRED[GROQ].update(ret); PROVIDERS[GROQ]["models"] = saved

# R4 key sai -> dừng ngay ở /models, không gọi thử model nào
cfg4 = {}
d4 = fk.FreeKeysDialog(cfg4); d4.show()
d4.rows[GROQ]["ed"].setText("gsk_bad" + "x" * 36); CALLS.clear(); d4._check(GROQ)
pump(10, lambda: d4.rows[GROQ]["btn"].isEnabled())
rec("R4", "Key sai: báo 'HTTP 401: key không đúng…' ngay, không gọi thử model",
    d4.rows[GROQ]["st"].text().startswith("✖ HTTP 401") and not [c for c in CALLS if c[0] == "POST"],
    f"'{d4.rows[GROQ]['st'].text()}', số lần gọi model={len([c for c in CALLS if c[0] == 'POST'])}")
for x in (d, d2, d3, d4):
    x.reject()
pump(0.2)

# R5 dịch bằng cấu hình cũ (không qua hộp thoại): make_provider tự đổi model
p_old = make_provider(GROQ, "gsk_" + "D" * 40, "llama-3.1-8b-instant")
out = p_old.chat("Dịch", "你好")
rec("R5", "Dịch với cấu hình cũ llama-3.1-8b-instant: tự gọi openai/gpt-oss-20b, không lỗi 404",
    p_old.model == "openai/gpt-oss-20b" and out == "Xin chào", f"model gọi={p_old.model}, kết quả='{out}'")

# R6 mở app với settings.json cũ
dd = Path(__file__).parent / "qa_data" / "retired_cfg"
shutil.rmtree(dd, ignore_errors=True); dd.mkdir(parents=True)
g.DATA = dd; g.PROMPTS = dd / "prompts"; g.CONFIG = dd / "settings.json"
g.CONFIG.write_text(json.dumps({"provider": GROQ, "per_provider": {GROQ: {
    "keys": "gsk_" + "E" * 40, "enabled": True, "model": "llama-3.3-70b-versatile",
    "models_cache": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"]}}}), encoding="utf-8")
w = g.MainWindow(); w.show(); w._go_page(1); w.cb_provider.setCurrentText(GROQ); pump(0.3)
items = [w.cb_model.itemText(i) for i in range(w.cb_model.count())]
w._on_models(["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b"], "")
items2 = [w.cb_model.itemText(i) for i in range(w.cb_model.count())]
rec("R6", "Mở app với settings.json cũ: ô Model hiện openai/gpt-oss-120b, danh sách không còn model đã tắt",
    w.cb_model.currentText() == "openai/gpt-oss-120b" and not any(m in GONE for m in items + items2),
    f"đang chọn={w.cb_model.currentText()}, danh sách={items}, sau 'Lấy model'={items2}")
w.close(); pump(0.2)

rec("R7", "Nhận đúng lỗi 'model không tồn tại' nhưng không nhầm với key sai/hết lượt",
    model_gone("HTTP 404: The model `x` does not exist or you do not have access to it.")
    and not model_gone("HTTP 401: Invalid API Key") and not model_gone("HTTP 429: Rate limit reached"), "404 model / 401 / 429")

from app.providers import RETIRED, current_model  # noqa: E402
bad_defaults = [(n, m) for n, c in PROVIDERS.items() for m in (c.get("models") or []) if m in RETIRED.get(n, {})]
maps = {
    ("NVIDIA NIM (miễn phí)", "deepseek-ai/deepseek-v3.1"): "moonshotai/kimi-k3.1-pro",
    ("SiliconFlow (miễn phí)", "THUDM/glm-4-9b-chat"): "Qwen/Qwen3-8B",
    ("Zhipu GLM (miễn phí)", "glm-4.5-flash"): "glm-4.7-flash",
    ("Cerebras (miễn phí)", "gemma-4-31b"): "qwen-3.8-27b",
    ("Groq (miễn phí)", "qwen/qwen3.6-27b"): "openai/gpt-oss-120b",
}
wrong = {k: current_model(*k) for k, v in maps.items() if current_model(*k) != v}
rec("R8", "Các nhà miễn phí khác: danh sách mặc định không còn model đã ngừng; model cũ tự đổi đúng",
    not bad_defaults and not wrong, f"model ngừng còn trong danh sách={bad_defaults}, đổi sai={wrong}")

print(f"\n{sum(RES)}/{len(RES)} PASS", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
