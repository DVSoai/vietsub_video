"""Máy chủ Bilibili giả lập dùng chung cho các bài kiểm thử tab Tìm & tải phim."""
import base64, json, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
CALLS = []


def arc(i, tag, style="rank"):
    it = {"bvid": f"BV1{tag}{i:05d}", "title": f"{tag} video {i} <em class=\"keyword\">x</em>", "duration": 95 + i,
          "pubdate": 1795168800, "stat": {"view": 10000 * (100 - i)}}
    if style == "rcmd":     # region/feed/rcmd: cover + author, không có pic/owner
        it.update({"cover": f"http://i1.hdslb.com/c/{tag}{i}.jpg", "author": {"name": f"up{i}"}, "goto": "av"})
    else:
        it.update({"pic": f"http://i2.hdslb.com/p/{tag}{i}.jpg", "owner": {"name": f"up{i}"}})
    return it


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        CALLS.append((u.path, q))
        if u.path.endswith(".jpg"):
            return self._send(PNG, "image/png")
        if u.path == "/x/frontend/finger/spi":
            return self._send({"code": 0, "data": {"b_3": "x", "b_4": "y"}})
        if u.path == "/x/web-interface/popular":
            pn = int(q.get("pn", 1))
            return self._send({"code": 0, "data": {"list": [arc(i + (pn - 1) * 20, "HOT") for i in range(20)],
                                                   "no_more": pn >= 3}})
        if u.path == "/x/web-interface/ranking/v2":
            rid = q.get("rid")
            if rid == "2005":
                return self._send({"code": -400, "message": "请求错误"})
            return self._send({"code": 0, "data": {"list": [arc(i, f"R{rid}") for i in range(94 if rid == "1021" else 100)]}})
        if u.path == "/x/web-interface/region/feed/rcmd":
            d = int(q.get("display_id", 1))
            return self._send({"code": 0, "data": {"archives": [arc(i + d * 100, f"C{q.get('from_region')}", "rcmd")
                                                                for i in range(15)]}})
        if u.path == "/x/web-interface/search/type":
            return self._send({"code": 0, "data": {"result": [{"type": "video", "bvid": "BV1SEARCH0001", "title": "短剧",
                               "author": "a", "pic": "//i0.hdslb.com/s.jpg", "duration": "4:46", "play": 5}],
                               "page": 1, "numPages": 1, "numResults": 1}})
        self._send({"code": 0, "data": {}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

