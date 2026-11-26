"""Tab 1 · Duyệt Bilibili không cần từ khóa: Thịnh hành, Bảng xếp hạng theo mục, Đề xuất trong mục.

Máy chủ giả lập trả dữ liệu đúng cấu trúc Bilibili thật (đã gọi thử thật trên GitHub Actions 26/11/2026).
Chạy: python tests/test_bilibili_browse.py
"""
from __future__ import annotations

import base64
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
RES = []
OUT = Path(__file__).parent / "qa_data"


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


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

from PySide6.QtWidgets import QApplication  # noqa: E402

import app.bilibili as bl  # noqa: E402
from app import theme  # noqa: E402
from app.search_page import SearchPage  # noqa: E402

_img = bl.Bili.image
bl.Bili.image = lambda self, url: _img(self, url.replace("https://i1.hdslb.com", BASE).replace("https://i2.hdslb.com", BASE)
                                       .replace("https://i0.hdslb.com", BASE))
app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


# ------------------------------------------------------------ B1 hàm gọi API
b = bl.Bili(api=BASE, web=BASE)
r_pop = b.popular(1)
r_rank = b.ranking(1001, 2)
r_last = b.ranking(1021, 5)
r_rc = b.region_rcmd(1021, 2)
v = r_rc.videos[0]
rec("B1", "Thịnh hành 20/trang; Xếp hạng 100 video chia 5 trang; Đề xuất đọc được cover/author; ảnh http->https",
    len(r_pop.videos) == 20 and r_pop.num_pages == 2 and len(r_rank.videos) == 20 and r_rank.num_pages == 5
    and r_rank.videos[0].bvid == "BV1R100100020" and len(r_last.videos) == 14 and len(r_rc.videos) == 15
    and v.author == "up200" and v.cover.startswith("https://") and v.duration == "4:55" and "<em" not in r_rank.videos[0].title,
    f"popular={len(r_pop.videos)}/{r_pop.num_pages} trang, rank 1001 trang 2 bắt đầu {r_rank.videos[0].bvid}, "
    f"1021 trang 5={len(r_last.videos)} video, rcmd={len(r_rc.videos)} ({v.author}, {v.duration}, {v.cover[:20]}…)")
n_rank_calls = len([c for c in CALLS if c[0] == "/x/web-interface/ranking/v2"])
b.ranking(1001, 3)
rec("B2", "Lật trang Xếp hạng không gọi lại Bilibili (dùng bản đã tải trong 10 phút)",
    len([c for c in CALLS if c[0] == "/x/web-interface/ranking/v2"]) == n_rank_calls, f"số lần gọi xếp hạng={n_rank_calls}")
try:
    b.ranking(2005)
    err = ""
except bl.BiliError as e:
    err = str(e)
rec("B3", "Mục bị Bilibili từ chối: báo lỗi rõ", "mã -400" in err, err)

# ------------------------------------------------------------ giao diện
dd = OUT / "browse_cfg"
shutil.rmtree(dd, ignore_errors=True)
cfg = {}
page = SearchPage(cfg, lambda: None, lambda m: None, lambda: [], bili=bl.Bili(api=BASE, web=BASE))
page.chk_tr.setChecked(False)
page.resize(1366, 640); page.show()
ok_auto = pump(6, lambda: len(page.cards) == 20)
rec("B4", "Mở tab lần đầu: tự hiện Thịnh hành, không phải gõ gì",
    ok_auto and page.source == ("browse", "popular", 0) and "Thịnh hành" in page.lb_count.text()
    and not page.cb_bcat.isEnabled(), f"thẻ={len(page.cards)}, nhãn='{page.lb_count.text()}', ô mục bật={page.cb_bcat.isEnabled()}")

page.cb_bmode.setCurrentText("🏆 Bảng xếp hạng")
pump(5, lambda: page.source[:2] == ("browse", "rank") and page.cards and page.cards[0].v.bvid.startswith("BV1R0"))
page.cb_bcat.setCurrentText("Phim ảnh · review, cắt phim (影视)")
pump(5, lambda: page.cards and page.cards[0].v.bvid.startswith("BV1R1001"))
pg = page.lb_page.text()
page.btn_next.click(); pump(5, lambda: page.page == 2)
second = page.cards[0].v.bvid if page.cards else ""
rec("B5", "Chọn Bảng xếp hạng + mục Phim ảnh: hiện top của mục, có Trang 1/5, Trang sau sang top 21–40",
    page.source == ("browse", "rank", 1001) and pg == "Trang 1 / 5" and page.page == 2 and second == "BV1R100100020"
    and "Phim ảnh" in page.lb_count.text() and "100 video" in page.lb_count.text(),
    f"{pg} -> trang {page.page}, thẻ đầu={second}, nhãn='{page.lb_count.text()}'")
pump(1.5); page._relayout(); pump(0.5)
page.grab().save(str(OUT / "tab_tim_phim_duyet_muc.png"))

page.cb_bmode.setCurrentText("✨ Đề xuất trong mục")
page.cb_bcat.setCurrentText("Tiểu kịch trường · phim ngắn tự diễn (小剧场)")
pump(5, lambda: page.cards and page.cards[0].v.bvid.startswith("BV1C1021"))
first_batch = page.cards[0].v.bvid if page.cards else ""
page.btn_next.click(); pump(5, lambda: page.page == 2)
rec("B6", "Đổi kiểu rồi đổi mục liền tay: hiện đúng mục cuối cùng (Tiểu kịch trường); Trang sau là loạt khác",
    page.source == ("browse", "rcmd", 1021) and first_batch.startswith("BV1C1021") and page.cards
    and page.cards[0].v.bvid.startswith("BV1C1021") and page.cards[0].v.bvid != first_batch
    and "Tiểu kịch trường" in page.lb_count.text(),
    f"loạt 1 bắt đầu {first_batch}, loạt 2 bắt đầu {page.cards[0].v.bvid if page.cards else None}")

imgs = pump(5, lambda: all(c.findChild(type(page.lb_count)) is not None for c in page.cards))
page.ed_q.setText("短剧"); page._search(1)
pump(5, lambda: page.source == ("search", "短剧") and page.cards and page.cards[0].v.bvid == "BV1SEARCH0001")
rec("B7", "Tìm theo từ khóa vẫn chạy như cũ sau khi duyệt", page.source == ("search", "短剧") and len(page.cards) == 1,
    f"nguồn={page.source}, thẻ={len(page.cards)}")

page.cb_site.setCurrentIndex(1); pump(0.2)
hidden = not page.cb_bmode.isVisible() and not page.btn_browse.isVisible()
page.cb_site.setCurrentIndex(0); pump(0.2)
shown = page.cb_bmode.isVisible()
page._store()
st = cfg.get("search", {})
rec("B8", "Trang khác Bilibili: ẩn hàng duyệt; nhớ lựa chọn duyệt cho lần mở sau",
    hidden and shown and st.get("browse_mode") == "✨ Đề xuất trong mục" and "小剧场" in st.get("browse_cat", ""),
    f"ẩn khi đổi trang={hidden}, hiện lại={shown}, lưu={st.get('browse_mode')} / {st.get('browse_cat')}")

cfg2 = {"search": {"browse_mode": "🏆 Bảng xếp hạng", "browse_cat": "Ẩm thực (美食)", "auto_browse": True}}
p2 = SearchPage(cfg2, lambda: None, lambda m: None, lambda: [], bili=bl.Bili(api=BASE, web=BASE))
p2.chk_tr.setChecked(False); p2.show()
pump(6, lambda: p2.cards and p2.cards[0].v.bvid.startswith("BV1R1020"))
rec("B9", "Mở lại app: tự hiện đúng mục đã chọn lần trước (Xếp hạng · Ẩm thực)",
    p2.source == ("browse", "rank", 1020) and len(p2.cards) == 20, f"nguồn={p2.source}, thẻ={len(p2.cards)}")
for x in (page, p2):
    x.close()
    x.dthread.shutdown() if hasattr(x.dthread, "shutdown") else None
print(f"\n{sum(RES)}/{len(RES)} PASS", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
