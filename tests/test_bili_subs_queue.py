"""Tab 1: lấy phụ đề Bilibili (đăng nhập QR, SRT/VTT/TXT/JSON, tải kèm .zh.srt) và hàng chờ tải
(tạm dừng / tiếp tục từ chỗ dừng / bỏ phim đang chờ / bỏ phim đang tải).

Máy chủ giả lập theo cấu trúc API thật của Bilibili (gọi thử thật 27/11/2026: khách gọi /x/player/wbi/v2
thì danh sách phụ đề rỗng và need_login_subtitle=true).
Chạy: python tests/test_bili_subs_queue.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NO_PROXY"] = "127.0.0.1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RES = []
OUT = Path(__file__).parent / "qa_data" / "subs_queue"
shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


MEDIA = os.urandom(3 * 1024 * 1024)          # mỗi luồng 3 MB, gửi chậm ~2 MB/s để kịp bấm tạm dừng
RANGES: list[tuple[str, int]] = []           # (đường dẫn, byte bắt đầu) của mọi lần tải luồng
POLLS = {"n": 0}
SUB_ZH = [{"from": 0.5, "to": 2.25, "content": "你好"}, {"from": 3, "to": 4.5, "content": "再见"}]
SUB_AI = [{"from": 0.5, "to": 2.0, "content": "AI 你好"}]
SUB_EN = [{"from": 0.5, "to": 2.0, "content": "Hello"}]
VIDEOS = {"BV1SUB000001": 1, "BV1SUB000002": 1, "BV1SUB000003": 1, "BV1MULTI0001": 2}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, headers=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _logged(self):
        return "SESSDATA=good" in (self.headers.get("Cookie") or "")

    def _media(self, path):
        start = 0
        rg = self.headers.get("Range")
        if rg:
            start = int(rg.split("=")[1].split("-")[0])
        RANGES.append((path, start))
        if start >= len(MEDIA):
            self.send_response(416); self.send_header("Content-Length", "0"); self.end_headers()
            return
        body = MEDIA[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024]); self.wfile.flush()
                time.sleep(0.03)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        if p.startswith("/media/"):
            return self._media(p)
        if p == "/x/frontend/finger/spi":
            return self._send({"code": 0, "data": {"b_3": "x", "b_4": "y"}})
        if p == "/x/web-interface/nav":
            d = {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                             "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"},
                 "isLogin": self._logged()}
            if self._logged():
                d.update({"uname": "tester", "vipStatus": 0})
            return self._send({"code": 0 if self._logged() else -101, "data": d})
        if p == "/x/passport-login/web/qrcode/generate":
            POLLS["n"] = 0
            return self._send({"code": 0, "data": {"url": "https://account.bilibili.com/h5/scan?k=K1", "qrcode_key": "K1"}})
        if p == "/x/passport-login/web/qrcode/poll":
            POLLS["n"] += 1
            code = {1: 86101, 2: 86090}.get(POLLS["n"], 0)
            hdr = {"Set-Cookie": "SESSDATA=good; Path=/; HttpOnly"} if code == 0 else {}
            return self._send({"code": 0, "data": {"code": code, "url": "", "message": ""}}, hdr)
        if p == "/x/web-interface/view":
            bv = q.get("bvid")
            n = VIDEOS.get(bv, 1)
            pages = [{"page": i, "cid": 100 + i, "part": f"Tập {i}", "duration": 60} for i in range(1, n + 1)]
            return self._send({"code": 0, "data": {"bvid": bv, "title": f"Phim {bv}", "cid": 101, "pages": pages,
                                                   "duration": 60, "pic": "", "owner": {"name": "up"}}})
        if p == "/x/player/playurl":
            bv = q.get("bvid")
            v = {"baseUrl": f"{BASE}/media/{bv}/v.m4s", "height": 1080, "codecs": "avc1.640032", "bandwidth": 1}
            a = {"baseUrl": f"{BASE}/media/{bv}/a.m4s", "bandwidth": 1}
            return self._send({"code": 0, "data": {"dash": {"video": [v], "audio": [a], "duration": 60}}})
        if p == "/x/player/wbi/v2":
            if not self._logged():
                return self._send({"code": 0, "data": {"need_login_subtitle": True, "subtitle": {"subtitles": []}}})
            subs = [{"lan": "zh-CN", "lan_doc": "中文（中国）", "type": 0, "subtitle_url": f"//127.0.0.1:{PORT}/sub/zh.json"},
                    {"lan": "ai-zh", "lan_doc": "中文（自动生成）", "type": 1, "subtitle_url": f"{BASE}/sub/ai.json"},
                    {"lan": "en-US", "lan_doc": "English", "type": 0, "subtitle_url": f"{BASE}/sub/en.json"}]
            return self._send({"code": 0, "data": {"need_login_subtitle": False, "subtitle": {"subtitles": subs}}})
        if p.startswith("/sub/"):
            body = {"zh.json": SUB_ZH, "ai.json": SUB_AI, "en.json": SUB_EN}[p.split("/")[-1]]
            return self._send({"font_size": 0.4, "body": body})
        self._send({"code": 0, "data": {}})


class Srv(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # app ngắt kết nối khi tạm dừng/bỏ: bình thường
        pass


srv = Srv(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import app.bilibili as bl  # noqa: E402
from app import theme  # noqa: E402
from app.bili_dialogs import LoginDialog, SubtitleDialog  # noqa: E402
from app.search_page import SearchPage  # noqa: E402
from app.secure_store import protect_config, restore_config  # noqa: E402

# phụ đề giả có url "//127.0.0.1:port" -> Bili sửa thành https://; máy chủ giả chỉ có http
_body = bl.Bili.subtitle_body
bl.Bili.subtitle_body = lambda self, t: _body(self, bl.SubTrack(t.lan, t.name, t.url.replace("https://", "http://"), t.ai))
# ghép hình + tiếng: không cần ffmpeg thật cho dữ liệu ngẫu nhiên
bl.Downloader._merge = lambda self, v, a, out: shutil.copyfile(v or a, out)

app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


def mk():
    return bl.Bili(api=BASE, web=BASE, passport=BASE)


# ------------------------------------------------------------ S1 đổi định dạng
srt = bl.subs_to_srt(SUB_ZH); vtt = bl.subs_to_vtt(SUB_ZH); txt = bl.subs_to_txt(SUB_ZH)
tracks = [bl.SubTrack("ai-zh", "a", "u1", True), bl.SubTrack("zh-CN", "b", "u2"), bl.SubTrack("en-US", "c", "u3")]
best = bl.best_tracks(tracks)
rec("S1", "Đổi phụ đề Bilibili sang SRT/VTT/TXT; mỗi ngôn ngữ ưu tiên bản người đăng làm hơn bản AI",
    srt.startswith("1\n00:00:00,500 --> 00:00:02,250\n你好\n") and "2\n00:00:03,000 --> 00:00:04,500\n再见" in srt
    and vtt.startswith("WEBVTT\n\n00:00:00.500 --> 00:00:02.250\n你好") and txt == "你好\n再见\n"
    and best["zh"].url == "u2" and best["en"].url == "u3" and list(best) == ["zh", "en"],
    f"srt dòng 2={srt.splitlines()[1]!r}, vtt dòng 3={vtt.splitlines()[2]!r}, chọn zh={best['zh'].lan}")

# ------------------------------------------------------------ S2 khách không có phụ đề
g = mk()
try:
    g.subtitles("BV1SUB000001", 101); err = ""
except bl.BiliError as e:
    err = str(e)
rec("S2", "Khách chưa đăng nhập: báo rõ cần đăng nhập để lấy phụ đề", "đăng nhập" in err, err)

# ------------------------------------------------------------ S3 đăng nhập QR
b = mk()
url, key = b.qr_start()
seq = [b.qr_poll(key) for _ in range(3)]
acc = b.account()
rec("S3", "Đăng nhập QR: chờ quét → đã quét → thành công, cookie SESSDATA vào phiên",
    seq == ["wait", "scanned", "ok"] and b.logged_in() and acc == {"login": True, "name": "tester", "vip": False},
    f"trạng thái={seq}, tài khoản={acc}")

# ------------------------------------------------------------ S4 phụ đề sau khi đăng nhập
ts = b.subtitles("BV1SUB000001", 101)
body = b.subtitle_body(ts[0])
rec("S4", "Đã đăng nhập: đủ 3 bản phụ đề, nhận ra bản AI, url // được sửa thành https",
    [t.lan for t in ts] == ["zh-CN", "ai-zh", "en-US"] and [t.ai for t in ts] == [False, True, False]
    and ts[0].url.startswith("https://") and body == SUB_ZH and "Tự động (AI)" in ts[1].label,
    f"{[(t.lan, t.ai) for t in ts]}, câu đầu={body[0]['content']}")

# ------------------------------------------------------------ S5 lưu đăng nhập an toàn
cfg = {"bili_login": {"cookies": json.dumps(b.login_cookies())}}
saved = protect_config(cfg)
back = restore_config(json.loads(json.dumps(saved)))
hidden = "good" not in json.dumps(saved) if sys.platform == "win32" else True
rec("S5", "Cookie đăng nhập: Windows mã hoá DPAPI khi lưu, mở lại giải mã đúng",
    hidden and json.loads(back["bili_login"]["cookies"]).get("SESSDATA") == "good",
    f"nền tảng={sys.platform}, khoá lưu={list(saved['bili_login'])}")

# ------------------------------------------------------------ S6 hộp thoại đăng nhập
lb = mk()
dlg = LoginDialog(lb)
dlg.timer.setInterval(100)
res = {}
dlg.finished.connect(lambda r: res.setdefault("r", r))
dlg.show()
pump(8, lambda: "r" in res)
has_qr = dlg.qr.pixmap() is not None and not dlg.qr.pixmap().isNull()
rec("S6", "Hộp thoại đăng nhập: hiện mã QR, tự hỏi Bilibili tới khi quét xong rồi đóng",
    res.get("r") == QDialog.Accepted and lb.logged_in(), f"kết quả={res.get('r')}, có ảnh QR={has_qr}, lần hỏi={POLLS['n']}")

# ------------------------------------------------------------ S7 hộp thoại phụ đề
sd = SubtitleDialog(b, "Phim một", [bl.Part(f"{BASE}/video/BV1SUB000001", "Phim một")], str(OUT), None)
sd.show()
pump(8, lambda: sd.btn_save.isEnabled())
cur = sd.cb_track.currentData()
path = sd.default_path("srt")
sd.save_to(path, "srt")
sd.cb_fmt.setCurrentIndex(3)
jpath = sd.default_path("json"); sd.save_to(jpath, "json")
ok_files = os.path.exists(path) and open(path, encoding="utf-8").read() == bl.subs_to_srt(SUB_ZH) \
    and json.load(open(jpath, encoding="utf-8"))["body"] == SUB_ZH
rec("S7", "Lấy phụ đề: chọn sẵn tiếng Trung do người đăng làm, xem trước, lưu SRT/JSON đúng tên video",
    cur is not None and cur.lan == "zh-CN" and "你好" in sd.view.toPlainText() and ok_files
    and path.endswith("Phim một [BV1SUB000001].zh.srt"),
    f"bản chọn={cur.lan if cur else None}, file={os.path.basename(path)}, {sd.cb_track.count()} bản")
sd.close()

# multi-part: tên file theo phần
sd2 = SubtitleDialog(b, "Bộ phim", [bl.Part(f"{BASE}/video/BV1MULTI0001?p=2", "P2 · Tập 2")], str(OUT), None)
sd2.show()
pump(8, lambda: sd2.btn_save.isEnabled())
p2 = sd2.default_path("srt")
rec("S8", "Video nhiều phần: tên file phụ đề có _p2 giống tên video tải về", p2.endswith("[BV1MULTI0001_p2].zh.srt"),
    os.path.basename(p2))
sd2.close()

# ------------------------------------------------------------ hàng chờ
page = SearchPage({"bili_login": {"cookies": json.dumps({"SESSDATA": "good"})}, "search": {"auto_browse": False}},
                  lambda: None, lambda m: None, lambda: [], bili=mk())
page.chk_tr.setChecked(False)
page.show()
pump(5, lambda: "tester" in page.lb_acc.text())
rec("S9", "Mở lại app: dùng lại đăng nhập đã lưu, hiện tên tài khoản", "tester" in page.lb_acc.text(), page.lb_acc.text())

out = str(OUT / "dl")
page.chk_subs.setChecked(True)
jobs = [bl.DownloadJob(f"{BASE}/video/BV1SUB00000{i}", f"Phim {i}", out, subs=True) for i in (1, 2, 3)]
for j in jobs:
    page._enqueue(j)
j1, j2, j3 = jobs
pump(10, lambda: j1.state == "running" and j1._phase.get("video", (0, 0))[0] > 300_000)
page.rows[id(j1)].btn_pause.click()
pump(5, lambda: j1.state == "paused")
part1 = os.path.join(out, "Phim 1 [BV1SUB000001].video.part")
size_paused = os.path.getsize(part1) if os.path.exists(part1) else 0
pump(5, lambda: j2.state == "running")
rec("Q1", "Tạm dừng phim đang tải: dừng ngay, giữ phần đã tải (.part), phim kế tiếp tự chạy",
    j1.state == "paused" and 0 < size_paused < len(MEDIA) and j2.state == "running"
    and page.rows[id(j1)].btn_resume.isVisible() and not page.rows[id(j1)].btn_pause.isVisible(),
    f"j1={j1.state} ({size_paused} byte), j2={j2.state}")

page.rows[id(j3)].btn_remove.click()
pump(0.5)
rec("Q2", "Bỏ phim đang chờ: biến khỏi danh sách, không bao giờ được tải",
    j3.state == "removed" and id(j3) not in page.rows, f"j3={j3.state}, còn {len(page.rows)} dòng")

pump(10, lambda: j2._phase.get("video", (0, 0))[0] > 300_000)
page.rows[id(j2)].btn_remove.click()
pump(5, lambda: j2.state == "removed" and id(j2) not in page.rows)
part2 = os.path.join(out, "Phim 2 [BV1SUB000002].video.part")
rec("Q3", "Bỏ phim đang tải: dừng, xoá phần tải dở, xoá khỏi danh sách",
    j2.state == "removed" and not os.path.exists(part2) and id(j2) not in page.rows, f"j2={j2.state}, .part còn={os.path.exists(part2)}")

n_before = len(RANGES)
page.rows[id(j1)].btn_resume.click()
pump(15, lambda: j1.state == "done")
resumed = [s for pth, s in RANGES[n_before:] if pth.endswith("BV1SUB000001/v.m4s")]
mp4 = os.path.join(out, "Phim 1 [BV1SUB000001].mp4")
zh = os.path.join(out, "Phim 1 [BV1SUB000001].zh.srt")
en = os.path.join(out, "Phim 1 [BV1SUB000001].en.srt")
rec("Q4", "Tiếp tục: tải tiếp từ chỗ dừng (Range), file đủ dung lượng, kèm phụ đề .zh.srt (bản người đăng) và .en.srt",
    j1.state == "done" and resumed and resumed[0] >= size_paused > 0 and os.path.getsize(mp4) == len(MEDIA)
    and open(zh, encoding="utf-8").read() == bl.subs_to_srt(SUB_ZH) and os.path.exists(en)
    and not any(pth.endswith("BV1SUB000003/v.m4s") for pth, _ in RANGES),
    f"j1={j1.state}, tải lại từ byte {resumed[:1]}, {j1.info}")

# tạm dừng phim đang chờ rồi tiếp tục
j4 = bl.DownloadJob(f"{BASE}/video/BV1SUB000002", "Phim 4", out)
j5 = bl.DownloadJob(f"{BASE}/video/BV1SUB000003", "Phim 5", out)
page._enqueue(j4); page._enqueue(j5)
pump(5, lambda: j4.state == "running")
page.rows[id(j5)].btn_pause.click()
pump(0.3)
paused_q = j5.state
page.rows[id(j5)].btn_resume.click()
pump(25, lambda: j5.state == "done")
rec("Q5", "Tạm dừng phim đang chờ (chưa tải) rồi Tiếp tục: xếp lại vào hàng chờ và tải xong",
    paused_q == "paused" and j4.state == "done" and j5.state == "done", f"j5 sau tạm dừng={paused_q}, cuối={j5.state}")
pump(1)
rec("Q6", "Dòng tổng kết đếm đúng (3 phim còn lại đều xong)", page.lb_qsum.text() == "3/3 xong", page.lb_qsum.text())

page.close()
page.dthread.shutdown()
print(f"\n{sum(RES)}/{len(RES)} PASS", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
