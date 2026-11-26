"""Kiểm thử nhà cung cấp miễn phí: giới hạn request theo từng key, bỏ <think>, cấu hình có sẵn.

Chạy: python tests/test_free_providers.py   (không cần mạng, dùng máy chủ giả lập cục bộ)
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.providers import PROVIDERS, make_provider, strip_thinking  # noqa: E402
from app.translator import parse_json_reply  # noqa: E402

RESULTS = []


def rec(tid, name, ok, detail):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


# ---------------------------------------------------------------- máy chủ giả lập kiểu OpenAI
STATE = {"arrivals": [], "running": 0, "max_running": 0}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        with LOCK:
            STATE["arrivals"].append(time.monotonic())
            STATE["running"] += 1
            STATE["max_running"] = max(STATE["max_running"], STATE["running"])
        time.sleep(0.3)                      # giả lập thời gian trả lời
        with LOCK:
            STATE["running"] -= 1
        body = json.dumps({"choices": [{"message": {"content":
               "<think>Người dùng muốn JSON {\"x\": 1} ... suy nghĩ</think>\n```json\n{\"1\": \"Xin chào\"}\n```"}}]})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body.encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# ---------------------------------------------------------------- F1 bỏ phần suy luận
cases = {
    "có think": ("<think>a {b} c</think>{\"1\":\"x\"}", "{\"1\":\"x\"}"),
    "thiếu thẻ mở": ("lý do...</think>\n{\"1\":\"x\"}", "{\"1\":\"x\"}"),
    "không think": ("{\"1\":\"x\"}", "{\"1\":\"x\"}"),
    "nhiều think": ("<think>1</think>A<think>2</think>B", "AB"),
}
res = {k: strip_thinking(i) == o for k, (i, o) in cases.items()}
rec("F1", "Bỏ <think>…</think> của model suy luận", all(res.values()), res)
try:
    parsed = parse_json_reply("<think>xem {\"bẫy\": 1} rồi</think>```json\n{\"1\": \"Xin chào\", \"2\": \"Tạm biệt\"}\n```")
    ok = parsed == {"1": "Xin chào", "2": "Tạm biệt"}
except Exception as e:  # noqa: BLE001
    ok, parsed = False, str(e)
rec("F2", "Đọc JSON khi phần suy luận có dấu {…}", ok, parsed)

# ---------------------------------------------------------------- F3 giới hạn mỗi key
PROVIDERS["_thu"] = {"kind": "openai", "base_url": BASE, "models": ["m"], "rpm": 120, "concurrency": 1}
p = make_provider("_thu", "k-1234", "m")
STATE.update(arrivals=[], running=0, max_running=0)
outs = []
t0 = time.monotonic()
ths = [threading.Thread(target=lambda: outs.append(p.chat("s", "u"))) for _ in range(5)]
[t.start() for t in ths]; [t.join() for t in ths]
gaps = [b - a for a, b in zip(STATE["arrivals"], STATE["arrivals"][1:])]
ok = STATE["max_running"] == 1 and min(gaps) >= 0.45 and all("<think>" not in o for o in outs)
rec("F3", "Giới hạn 120 rpm + 1 request cùng lúc cho một key", ok,
    f"cùng lúc tối đa={STATE['max_running']}, khoảng cách giữa các request={[round(g, 2) for g in gaps]}s "
    f"(≥0.5s), tổng {time.monotonic() - t0:.1f}s cho 5 request")

PROVIDERS["_thu2"] = {"kind": "openai", "base_url": BASE, "models": ["m"], "rpm": 0, "concurrency": 3}
p2 = make_provider("_thu2", "k", "m")
STATE.update(arrivals=[], running=0, max_running=0)
ths = [threading.Thread(target=lambda: p2.chat("s", "u")) for _ in range(6)]
[t.start() for t in ths]; [t.join() for t in ths]
rec("F4", "Giới hạn 3 request cùng lúc", STATE["max_running"] == 3, f"cùng lúc tối đa={STATE['max_running']}")

# hai key khác nhau của cùng nhà cung cấp không chặn lẫn nhau
pa, pb = make_provider("_thu", "key-A", "m"), make_provider("_thu", "key-B", "m")
STATE.update(arrivals=[], running=0, max_running=0)
ths = [threading.Thread(target=x.chat, args=("s", "u")) for x in (pa, pb)]
[t.start() for t in ths]; [t.join() for t in ths]
rec("F5", "Hai key riêng chạy song song (giới hạn tính theo từng key)", STATE["max_running"] == 2,
    f"cùng lúc tối đa={STATE['max_running']}")
del PROVIDERS["_thu"], PROVIDERS["_thu2"]

# ---------------------------------------------------------------- F6 cấu hình có sẵn
free = {k: v for k, v in PROVIDERS.items() if v.get("free")}
bad = [k for k, v in free.items() if not (v["base_url"].startswith("https://") and v["models"] and v.get("signup")
                                         and v.get("rpm") and v.get("concurrency"))]
made = []
for k, v in free.items():
    pr = make_provider(k, "test-key", v["models"][0])
    made.append(pr.base_url == v["base_url"] and pr.min_interval == 60 / v["rpm"])
rec("F6", "Nhà cung cấp miễn phí có đủ URL, model, link lấy key, giới hạn", not bad and all(made),
    f"{list(free)}; thiếu={bad or 'không'}")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} PASS")
sys.exit(0 if all(RESULTS) else 1)
