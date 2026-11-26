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
PROVIDERS["Groq (miễn phí)"]["base_url"] = BASE      # kiểm tra Groq giờ lấy địa chỉ từ cấu hình
fk.CHECK_TIMEOUT = 3

BOXES = []
ANSWER = {"q": QMessageBox.No}
QMessageBox.question = staticmethod(lambda *a, **k: (BOXES.append(("question", a[2] if len(a) > 2 else "")), ANSWER["q"])[1])
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
ok3 = r["st"].text().startswith("✔") and "tự chuyển" in r["st"].text() and \
    cfg["per_provider"][GROQ].get("model") in PROVIDERS[GROQ]["models"]
rec("A1", "Groq: key đúng -> bật; key sai (401) -> không lưu; model không có -> tự chuyển sang model còn chạy", ok1 and ok2 and ok3,
    f"đúng={ok1}, sai='{bad_txt}', model lạ='{r['st'].text()}'")

# ---------------------------------------------------------------- A2/A3 đóng hộp thoại khi đang kiểm tra, hết giờ
SLOW["sec"] = 30
cfgc = {}
dc = fk.FreeKeysDialog(cfgc); dc.show()
dc.rows[NV]["ed"].setText("nvapi-" + "c" * 40); dc._check(NV)
pump(0.3)
t0 = time.time(); dc.reject(); pump(0.2)
closed = not dc.isVisible(); close_lag = time.time() - t0
pump(fk.CHECK_TIMEOUT + 2, lambda: not fk._ORPHANS)
rec("A2", "Đóng hộp thoại khi đang kiểm tra: đóng ngay, key đang thử không được lưu, luồng nền tự kết thúc",
    closed and close_lag < 1 and not cfgc.get("per_provider", {}).get(NV, {}).get("keys") and not fk._ORPHANS,
    f"đóng sau {close_lag:.2f}s, key lưu={cfgc.get('per_provider', {}).get(NV, {}).get('keys')!r}, luồng còn lại={len(fk._ORPHANS)}")
d3 = fk.FreeKeysDialog({}); d3.show()
d3.rows[NV]["ed"].setText("nvapi-" + "t" * 40); t0 = time.time(); d3._check(NV)
pump(fk.CHECK_TIMEOUT + 5, lambda: d3.rows[NV]["btn"].isEnabled())
rec("A3", f"Nhà cung cấp không trả lời: báo hết giờ sau ~{fk.CHECK_TIMEOUT}s (bản app: 20s), không treo 180s",
    time.time() - t0 < fk.CHECK_TIMEOUT + 3 and "Không có phản hồi" in d3.rows[NV]["st"].text(),
    f"{time.time() - t0:.1f}s, '{d3.rows[NV]['st'].text()}'")
d3.reject(); pump(0.2)
SLOW["sec"] = 0

# ---------------------------------------------------------------- A4 thông báo lỗi nhà không phải Groq
cfg2 = {}
d2 = fk.FreeKeysDialog(cfg2); d2.show()
d2.rows[NV]["ed"].setText("nvapi-bad" + "z" * 30); d2._check(NV)
pump(8, lambda: d2.rows[NV]["btn"].isEnabled())
msg = d2.rows[NV]["st"].text()
rec("A4", "Key NVIDIA sai: báo 'HTTP 401: key không đúng…', không lưu, không lộ key", msg.startswith("✖ HTTP 401") and
    "nvapi-bad" not in msg and
    not cfg2.get("per_provider", {}).get(NV, {}).get("keys"), f"thông báo người dùng thấy: '{msg}'")
d2.reject(); pump(0.2)
fe = fk.friendly_error(Exception("HTTP 429: Rate limit for key sk-SECRETKEY reached"), "sk-SECRETKEY")
fe2 = fk.friendly_error(Exception("Lỗi mạng: weird failure sk-SECRETKEY"), "sk-SECRETKEY")
rec("A4b", "Thông báo lỗi dễ hiểu theo mã HTTP và không bao giờ chứa key", fe.startswith("HTTP 429: hết lượt")
    and "SECRET" not in fe and "SECRET" not in fe2, f"429 → '{fe}', mạng → '{fe2}'")

# ---------------------------------------------------------------- A4c copy key đúng lúc đang kiểm tra
SLOW["sec"] = 1.5
cfgk = {}
dk = fk.FreeKeysDialog(cfgk); dk.show(); dk.chk_watch.setChecked(True)
dk.rows[NV]["ed"].setText("nvapi-" + "a" * 40); dk._check(NV); pump(0.2)
from PySide6.QtGui import QGuiApplication  # noqa: E402
QGuiApplication.clipboard().setText("nvapi-" + "b" * 40)       # copy key mới khi NVIDIA đang bận
pump(6, lambda: "bbbb" in cfgk.get("per_provider", {}).get(NV, {}).get("keys", ""))
got = cfgk.get("per_provider", {}).get(NV, {}).get("keys", "")
rec("A4c", "Copy key mới đúng lúc nhà đó đang kiểm tra: không bị mất, kiểm tra xong thì tự nhận tiếp",
    "aaaa" in got and "bbbb" in got, f"key đã lưu: {['…' + k[-4:] for k in got.splitlines()]}")
dk.chk_watch.setChecked(False); pump(0.8); dk.reject(); pump(0.2)
SLOW["sec"] = 0

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
w.cfg.setdefault("per_provider", {})[GROQ] = {"keys": "gsk_" + "K" * 40, "enabled": True, "model": "openai/gpt-oss-120b"}
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
w2.ai_provider.setCurrentText(NV); pump(0.1)
w2.ai_model.setCurrentText("deepseek-ai/deepseek-v3.1"); w2.ai_keys.setText("nvapi-" + "e" * 40); w2.ai_enabled.setChecked(True)
w2._save_ai_editor(); pump(0.1)
pe = dict(w2.cfg["per_provider"][NV])
in_tab2 = [p.label for p in w2._make_translate_providers()]
ANSWER["q"] = QMessageBox.Yes; w2._remove_ai_keys(); pump(0.1)
pr = dict(w2.cfg["per_provider"][NV])
rec("A7b", "Tab 5: lưu model/key/bật cho NVIDIA -> tab Dịch dùng ngay; Xoá toàn bộ key -> tắt và hết key",
    pe.get("keys", "").endswith("eeee") and pe.get("enabled") and any("NVIDIA" in l for l in in_tab2)
    and pr.get("keys") == "" and not pr.get("enabled") and w2.ai_keys.text() == "",
    f"sau lưu: bật={pe.get('enabled')}, model={pe.get('model')}; tab Dịch={in_tab2}; sau xoá: key={pr.get('keys')!r}")
ANSWER["q"] = QMessageBox.No
w2.close(); pump(0.3)

# ---------------------------------------------------------------- A8 file cấu hình không giải mã được
bad = json.loads(raw)
bad["refine"] = True
bad.setdefault("per_provider", {})[GROQ] = {"keys_dpapi": "AAAA", "enabled": True, "model": "openai/gpt-oss-20b"}
g.CONFIG.write_text(json.dumps(bad), encoding="utf-8")
before = g.CONFIG.read_text(encoding="utf-8")
BOXES.clear(); ANSWER["q"] = QMessageBox.No
w3 = g.MainWindow(); w3._save_cfg(); w3.close(); pump(0.3)
after = g.CONFIG.read_text(encoding="utf-8")
rec("A8", "Key không giải mã được, chọn No: hỏi rõ tên nhà, KHÔNG ghi đè file cấu hình",
    BOXES and BOXES[0][0] == "question" and "Groq" in BOXES[0][1] and before == after,
    f"hỏi={BOXES[0][1][:70] if BOXES else None!r}…, file giữ nguyên={before == after}")
BOXES.clear(); ANSWER["q"] = QMessageBox.Yes
w4 = g.MainWindow()
pc4 = w4.cfg.get("per_provider", {}).get(GROQ, {})
kept = w4.cfg.get("refine") is True and pc4.get("model") == "openai/gpt-oss-20b"
w4._save_cfg(); w4.close(); pump(0.3)
saved = json.loads(g.CONFIG.read_text(encoding="utf-8"))
bak = g.CONFIG.with_name("settings.json.bak")
rec("A8b", "Chọn Yes: bỏ key hỏng, giữ mọi cài đặt khác, sao lưu file cũ, app lưu lại bình thường",
    kept and not pc4.get("keys") and not pc4.get("enabled") and bak.exists() and bak.read_text(encoding="utf-8") == before
    and "keys_dpapi" not in json.dumps(saved.get("per_provider", {}).get(GROQ, {})) and saved.get("refine") is True,
    f"giữ cài đặt khác={kept}, key Groq={pc4.get('keys')!r}, bật={pc4.get('enabled')}, có .bak={bak.exists()}")

print(f"\n{sum(RES)}/{len(RES)} PASS  ({'Windows' if WIN else sys.platform})", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
