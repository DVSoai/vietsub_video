import json, random, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
random.seed(1)
stats = {"calls": 0, "429": 0, "dropped": 0}
lock = threading.Lock()
def fake_translate(user):
    items = json.loads(user)
    out = {}
    for k, v in items.items():
        if random.random() < 0.08:
            with lock: stats["dropped"] += 1
            continue
        out[k] = "Câu dịch #" + k + ("\n(dòng 2)" if "\n" in v else "")
    return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj, headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/stats": return self._send(200, stats)
        if self.path.startswith("/oa/models"): return self._send(200, {"data": [{"id": "mock-a"}, {"id": "models/mock-b"}]})
        if self.path.startswith("/an/models"): return self._send(200, {"data": [{"id": "claude-mock"}]})
        self._send(404, {})
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["content-length"]))
        if self.path == "/token":
            assert b"grant_type=urn" in raw and b"assertion=" in raw
            with lock: stats["tokens"] = stats.get("tokens", 0) + 1
            return self._send(200, {"access_token": "tok123", "expires_in": 3600, "token_type": "Bearer"})
        body = json.loads(raw)
        import time as _t; _t.sleep(float(__import__("os").environ.get("SLOW", "0")))
        with lock: stats["calls"] += 1
        if random.random() < 0.1:
            with lock: stats["429"] += 1
            return self._send(429, {"error": {"message": "rate limited"}}, {"retry-after": "0.2"})
        if self.headers.get("authorization") == "Bearer badkey":
            return self._send(401, {"error": {"message": "Incorrect API key provided"}})
        if self.path == "/oa/chat/completions":
            assert self.headers["authorization"].startswith("Bearer ")
            txt = fake_translate(body["messages"][1]["content"])
            return self._send(200, {"choices": [{"message": {"content": txt}}]})
        if self.path == "/vsa/projects/proj-abc/locations/us-central1/publishers/google/models/gemini-x:generateContent":
            if self.headers.get("authorization") != "Bearer tok123":
                return self._send(401, {"error": {"message": "bad token"}})
            txt = fake_translate(body["contents"][0]["parts"][0]["text"])
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": txt}]}}]})
        if self.path.startswith("/vxa/publishers/google/models/gemini-x:generateContent?key=k"):
            parts = body["contents"][0]["parts"]
            audio = [p for p in parts if "inlineData" in p]
            if audio:
                assert audio[0]["inlineData"]["mimeType"] == "audio/mp3" and len(audio[0]["inlineData"]["data"]) > 1000
                assert body["generationConfig"].get("responseMimeType") == "application/json"
                with lock: stats["audio"] = stats.get("audio", 0) + 1
                segs = [{"s": 0.5, "e": 2.8, "t": "师尊，弟子来迟了。", "g": "nam", "spk": "Nam 1", "emo": "kính cẩn"},
                        {"s": 3.2, "e": 4.2, "t": "嗯。", "g": "nữ", "spk": "Nữ 1", "emo": "lạnh lùng"},
                        {"s": "00:04.5", "e": "00:06.9", "t": "林澈，你可知罪？"},
                        {"s": 30.2, "e": 29.0, "t": "时间反了"},
                        {"s": 500, "e": 510, "t": "超出长度"}]
                return self._send(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(segs, ensure_ascii=False)}]}}]})
            sys_t = ((body.get("systemInstruction") or {}).get("parts") or [{}])[0].get("text", "")
            if "biên tập viên lồng tiếng" in sys_t:
                import re as _re
                ids = _re.findall(r"^(\d+)\. ", parts[0]["text"], _re.M)
                lines = {i: {"speaker": ("Lâm Triệt" if int(i) % 2 else "Sư tôn"), "emotion": ("cười" if int(i) % 5 == 0 else "bình thường")} for i in ids}
                out = {"characters": [{"name": "Lâm Triệt", "gender": "nam", "desc": "đệ tử trẻ"},
                                      {"name": "Sư tôn", "gender": "nữ", "desc": "sư phụ, giọng lạnh"}], "lines": lines}
                return self._send(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(out, ensure_ascii=False)}]}}]})
            txt = fake_translate(parts[0]["text"])
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": txt}]}}]})
        if self.path.startswith("/vx/publishers/google/models/gemini-x:generateContent?key=k"):
            if "safetySettings" in body:
                return self._send(400, {"error": {"message": "Unsupported safetySettings threshold OFF"}})
            assert body["systemInstruction"]["parts"][0]["text"]
            txt = fake_translate(body["contents"][0]["parts"][0]["text"])
            return self._send(200, {"candidates": [{"content": {"parts": [{"text": "suy nghĩ", "thought": True}, {"text": txt}]}}]})
        if self.path == "/an/messages":
            assert self.headers["x-api-key"]
            txt = fake_translate(body["messages"][0]["content"])
            return self._send(200, {"content": [{"type": "text", "text": txt}]})
        self._send(404, {})
srv = ThreadingHTTPServer(("127.0.0.1", 8765), H)
if __name__ == "__main__":
    srv.serve_forever()
