"""Giao diện mới: thanh điều hướng dọc, thanh hành động luôn thấy, Cắt video, bảng sửa phụ đề,
và chế độ Tự động từ link chạy trọn quy trình (Bilibili + AI giả lập; tải, ghép, in phụ đề bằng ffmpeg thật).

Chạy: python tests/test_ui_redesign.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
OUT = Path(__file__).parent / "qa_data" / "ui_redesign"
shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True, exist_ok=True)


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


import imageio_ffmpeg  # noqa: E402

FF = imageio_ffmpeg.get_ffmpeg_exe()
MEDIA = OUT / "src.mp4"
subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
                "-i", "sine=frequency=440", "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(MEDIA)], check=True)
MEDIA_BYTES = MEDIA.read_bytes()
SUB_ZH = [{"from": 0.5, "to": 2.5, "content": "师兄，等等我"}, {"from": 3, "to": 5, "content": "这是千年灵草"},
          {"from": 6, "to": 8, "content": "快走"}]
AI_CALLS = []
AI_DELAY = {"s": 0.0}
MEDIA_GETS = []


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json", headers=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = dict(urllib.parse.parse_qsl(u.query)); p = u.path
        logged = "SESSDATA=good" in (self.headers.get("Cookie") or "")
        if p.startswith("/media/"):
            MEDIA_GETS.append(p)
            return self._send(MEDIA_BYTES, "video/mp4")
        if p == "/x/frontend/finger/spi":
            return self._send({"code": 0, "data": {"b_3": "x"}})
        if p == "/x/web-interface/nav":
            return self._send({"code": 0, "data": {"isLogin": logged, "uname": "tester", "vipStatus": 0,
                               "wbi_img": {"img_url": "https://x/" + "a" * 32 + ".png", "sub_url": "https://x/" + "b" * 32 + ".png"}}})
        if p == "/x/web-interface/view":
            return self._send({"code": 0, "data": {"bvid": q.get("bvid"), "title": "Phim thử tự động", "cid": 7,
                               "pages": [{"page": 1, "cid": 7, "part": "P1", "duration": 12}], "duration": 12, "pic": ""}})
        if p == "/x/player/playurl":
            v = {"baseUrl": f"{BASE}/media/v.m4s", "height": 360, "codecs": "avc1.64001e", "bandwidth": 1}
            a = {"baseUrl": f"{BASE}/media/a.m4s", "bandwidth": 1}
            return self._send({"code": 0, "data": {"dash": {"video": [v], "audio": [a], "duration": 12}}})
        if p == "/x/player/wbi/v2":
            subs = [{"lan": "ai-zh", "lan_doc": "中文（自动生成）", "type": 1, "subtitle_url": f"{BASE}/sub/zh.json"}] if logged else []
            return self._send({"code": 0, "data": {"need_login_subtitle": not logged, "subtitle": {"subtitles": subs}}})
        if p == "/sub/zh.json":
            return self._send({"body": SUB_ZH})
        self._send({"code": 0, "data": {}})

    def do_POST(self):                                   # AI kiểu OpenAI: dịch theo batch JSON
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][-1]["content"]
        AI_CALLS.append(user)
        time.sleep(AI_DELAY["s"])
        data = json.loads(user[user.find("{"):user.rfind("}") + 1])
        out = {k: f"Câu dịch {k}" for k in data}
        resp = {"choices": [{"message": {"content": json.dumps(out, ensure_ascii=False)}}]}
        self._send(resp)


class Srv(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


srv = Srv(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

from PySide6.QtWidgets import QApplication  # noqa: E402

import app.bilibili as bl  # noqa: E402
import app.gui as g  # noqa: E402
from app import srt, theme  # noqa: E402
from app.sub_editor import SubtitleEditor, find_video, problems  # noqa: E402

bl.Bili.subtitle_body = (lambda orig: lambda self, t: orig(self, bl.SubTrack(t.lan, t.name, t.url.replace("https://", "http://"), t.ai)))(bl.Bili.subtitle_body)
app = QApplication(sys.argv); app.setStyle("Fusion"); app.setFont(theme.app_font()); app.setStyleSheet(theme.stylesheet())


def pump(sec, until=None):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.01)
        if until and until():
            return True
    return False


dd = OUT / "cfg"
g.DATA = dd; g.PROMPTS = dd / "prompts"; g.CONFIG = dd / "settings.json"
CUSTOM = "Tùy chỉnh (chuẩn OpenAI)"
w = g.MainWindow()
w.cfg.setdefault("search", {})["auto_browse"] = False
w.cfg.setdefault("per_provider", {})[CUSTOM] = {"keys": "k", "model": "m", "base_url": BASE, "enabled": True}
w._current_provider = None; w._on_provider(w.cb_provider.currentText())
w.resize(1366, 768); w.show(); pump(0.5)

# ------------------------------------------------------------ U1 điều hướng + thanh hành động
seen = []
for i, b in enumerate(w.nav_btns):
    b.click(); pump(0.05)
    seen.append((w.stack.currentIndex() == i, w.lb_page.text()))
w._go_page(1); pump(0.3)
r = w.btn_run.rect(); bottom = w.btn_run.mapTo(w, r.bottomLeft()).y()
rec("U1", "7 nút điều hướng dọc đổi đúng trang + tiêu đề; nút “Bắt đầu dịch” luôn thấy ở 1366×768 (không phải cuộn)",
    all(ok for ok, _ in seen) and len({t for _, t in seen}) == 7 and w.btn_run.isVisible() and bottom <= w.height(),
    f"tiêu đề={[t for _, t in seen]}, đáy nút={bottom}/{w.height()}")

# ------------------------------------------------------------ U2 dòng AI ở tab Dịch + thẻ Tốc độ thu gọn
short = w.lb_ai_short.text()
w.btn_ai_go.click(); pump(0.1)
went = w.stack.currentIndex() == 4
w._go_page(1)
collapsed = not w.speed_body.isVisible() and "luồng" in w.lb_speed.text()
w.btn_speed.click(); pump(0.1)
opened = w.speed_body.isVisible()
w.btn_speed.click()
rec("U2", "Tab Dịch: một dòng tóm tắt AI đang dùng + nút sang Cài đặt AI; thẻ Tốc độ thu gọn sẵn, bấm để mở",
    "Tùy chỉnh" in short and "m</span>" in short and went and collapsed and opened, f"tóm tắt='{short}', Tốc độ='{w.lb_speed.text()}'")

# ------------------------------------------------------------ U3 Cắt video
sp = w.split_page
sp.set_source(str(MEDIA)); sp.mode.setCurrentIndex(0); sp.value.setValue(5); pump(0.1)
rows = sp.table.rowCount()
sp.folder.setText(str(OUT / "cut")); sp.quality.setCurrentIndex(1)
sp.run_split()
pump(60, lambda: sp.worker is None)
parts = sorted(Path(sp.out_dir).glob("part_*.mp4")) if sp.out_dir else []
rec("U3", "Cắt video: xem trước 3 đoạn (12 s / 5 s), cắt ra đúng 3 file, nút Mở thư mục bật",
    rows == 3 and len(parts) == 3 and sp.btn_open.isEnabled() and sp.status.text().startswith("Hoàn tất"),
    f"bảng={rows} dòng, file={[x.name for x in parts]}, trạng thái='{sp.status.text()[:60]}'")

# ------------------------------------------------------------ U4 bảng sửa phụ đề
st1 = problems("这是千年灵草", "这是千年灵草", "00:00:01,000", "00:00:02,000", None)
st2 = problems("Đi mau, bọn chúng đuổi tới nơi rồi, không kịp nữa đâu!", "快走", "00:00:01,000", "00:00:01,800", None)
st3 = problems("", "好", "00:00:01,000", "00:00:02,000", None)
st4 = problems("Được", "好", "00:00:01,000", "00:00:03,000", "00:00:02,500")
st5 = problems("Được", "好", "00:00:01,000", "00:00:02,000", "00:00:03,000")
rec("U4", "Kiểm tra câu: còn chữ gốc / đọc quá nhanh / trống / chồng câu sau / câu ổn",
    st1[0] == "err" and st2[0] == "warn" and st3[0] == "err" and st4 == ("warn", ["chồng câu sau"]) and st5 == ("", []),
    f"{st1}, {st2}, {st3}, {st4}, {st5}")

vid = OUT / "ed.mp4"; shutil.copyfile(MEDIA, vid)
zh, vi = OUT / "ed.zh.srt", OUT / "ed.vi.srt"
zh.write_text("1\n00:00:00,500 --> 00:00:02,000\n师兄\n\n2\n00:00:02,200 --> 00:00:04,000\n灵草\n", encoding="utf-8")
vi.write_text("1\n00:00:00,500 --> 00:00:02,000\nSư huynh\n\n2\n00:00:02,200 --> 00:00:04,000\n灵草\n", encoding="utf-8")
dlg = SubtitleEditor(str(vi), str(zh), find_video(str(vi)), None, "Tiếng Việt", w)
dlg.show(); pump(0.2)
before = dlg._problem_rows()
dlg.table.selectRow(1); pump(0.1)
dlg.ed_text.setPlainText("Linh thảo"); pump(0.1)
after = dlg._problem_rows()
dlg.table.item(0, 1).setText("sai giờ"); pump(0.05)
kept = dlg.cues[0].start
dlg.table.item(0, 2).setText("00:00:01.9"); pump(0.05)
got_preview = pump(15, lambda: dlg.preview.pixmap() is not None and not dlg.preview.pixmap().isNull())
dlg.save(); pump(0.1)
saved = srt.parse(srt.read_text(str(vi)))
bak = Path(str(vi) + ".bak").exists()
dlg.dirty = False; dlg.reject(); pump(0.2)
rec("U5", "Bảng sửa phụ đề: câu còn chữ Trung tô đỏ, sửa xong hết đỏ; giờ sai bị từ chối, giờ đúng được chuẩn hoá; "
    "xem trước có hình; lưu ra file + giữ bản .bak",
    find_video(str(vi)) == str(vid) and before == [1] and after == [] and kept == "00:00:00,500"
    and saved[1].text == "Linh thảo" and saved[0].end == "00:00:01,900" and bak and got_preview,
    f"cần xem trước/sau={before}/{after}, giờ giữ={kept}, kết thúc câu 1={saved[0].end}, xem trước={got_preview}, .bak={bak}")

# ------------------------------------------------------------ U6 Tự động từ link: chạy trọn quy trình
w.search_page.bili = bl.Bili(api=BASE, web=BASE, passport=BASE)
w.search_page.bili.set_login({"SESSDATA": "good"})
ap = w.auto_page
w._go_page(6); pump(0.2)
ap.ed_out.setText(str(OUT / "auto"))
ap.cb_result.setCurrentIndex(0)
ap.ed_link.setText("xem phim này nè BV1AUTO00001 hay lắm")
ap.start()
pump(240, lambda: not ap.busy() and ap.res_box.isVisible())
states = {k: r.state for k, r in ap.rows.items()}
res = ap.result
out_ok = bool(res.get("out")) and os.path.exists(res["out"]) and os.path.getsize(res["out"]) > 10_000
vi_cues = srt.parse(srt.read_text(res["vi"])) if res.get("vi") else []
w.grab().save(str(OUT / "tu_dong.png"))
rec("U6", "Tự động: tải (kèm phụ đề AI Bilibili) → bỏ qua nghe giọng → dịch → in phụ đề ra .vietsub.mp4",
    states == {"download": "done", "subs": "done", "listen": "skip", "translate": "done", "export": "done"}
    and out_ok and [c.text for c in vi_cues] == ["Câu dịch 1", "Câu dịch 2", "Câu dịch 3"] and len(AI_CALLS) >= 1
    and ap.lb_status.text().startswith("✔"),
    f"bước={states}, kết quả={ {k: Path(v).name for k, v in res.items()} }, trạng thái='{ap.lb_status.text()}'")

# chạy lại cùng link: dùng lại video + phụ đề đã có, vẫn xong
n_ai, n_media = len(AI_CALLS), len(MEDIA_GETS)
ap.start()
pump(240, lambda: not ap.busy() and ap.lb_status.text().startswith(("✔", "✖")))
rec("U7", "Chạy lại cùng link: không tải lại video, không gọi AI dịch lại (dùng bản dịch đã có), vẫn ra video",
    ap.lb_status.text().startswith("✔") and len(MEDIA_GETS) == n_media and ap.rows["download"].state == "done"
    and len(AI_CALLS) == n_ai and "dùng lại bản dịch" in ap.rows["translate"].lb_info.toolTip(),
    f"trạng thái='{ap.lb_status.text()}', tải video thêm={len(MEDIA_GETS) - n_media} lần, AI gọi thêm={len(AI_CALLS) - n_ai}")

# dừng giữa chừng
shutil.rmtree(OUT / "auto2", ignore_errors=True)
ap.ed_out.setText(str(OUT / "auto2"))
AI_DELAY["s"] = 4.0                      # AI trả lời chậm để kịp bấm Dừng lúc đang dịch
ap.start()
pump(30, lambda: ap.rows["translate"].state == "now")
ap.stop()
pump(60, lambda: not ap.busy() and ap.lb_status.text().startswith(("Đã dừng", "✔", "✖")))
AI_DELAY["s"] = 0.0
rec("U8", "Bấm Dừng khi đang chạy: dừng hẳn, báo đã dừng, nút Bắt đầu bật lại",
    not ap.busy() and ap.lb_status.text().startswith("Đã dừng") and ap.btn_run.isEnabled(), ap.lb_status.text())

w.close(); pump(0.3)
print(f"\n{sum(RES)}/{len(RES)} PASS", flush=True)
sys.stdout.flush()
os._exit(0 if all(RES) else 1)
