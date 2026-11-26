"""Kiểm thử bảng thuật ngữ + dịch kỹ (thêm lượt biên tập), chạy với máy chủ AI giả lập kiểu OpenAI.

Chạy: python tests/test_translation_quality.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import srt  # noqa: E402
from app.providers import PROVIDERS, make_provider  # noqa: E402
from app.translator import Settings, Translator, extract_glossary, glossary_for, glossary_miss  # noqa: E402

RES = []


def rec(tid, name, ok, detail):
    RES.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid} {name}: {detail}", flush=True)


LOG: list[dict] = []
MODE = {"obey_glossary_after": 1}       # lần dịch thứ mấy mới dùng đúng thuật ngữ


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        LOG.append({"system": system, "user": user})
        if "THUẬT NGỮ cần dịch" in system:                      # lập bảng thuật ngữ
            out = {"terms": [{"src": "林澈", "vi": "Lâm Triệt", "loai": "nhân vật"},
                             {"src": "师尊", "vi": "sư tôn", "loai": "xưng hô"},
                             {"src": "不存在", "vi": "bịa", "loai": "?"},           # không có trong phụ đề -> phải bị bỏ
                             {"src": "林澈", "vi": "Lâm Triệt", "loai": "trùng"}]}
            txt = json.dumps(out, ensure_ascii=False)
        elif "biên tập viên phụ đề" in system:                 # lượt biên tập
            data = json.loads(user[user.find("{"):])
            txt = json.dumps({k: v["nhap"] + " (đã biên tập)" for k, v in data.items()}, ensure_ascii=False)
        else:                                                   # dịch
            data = json.loads(user[user.find("{"):])
            n_calls = sum(1 for x in LOG if "biên tập viên" not in x["system"] and "THUẬT NGỮ cần" not in x["system"])
            obey = n_calls > MODE["obey_glossary_after"]
            out = {}
            for k, v in data.items():
                t = v.replace("林澈", "Lâm Triệt" if obey else "Lin Che").replace("师尊", "sư tôn" if obey else "sư phụ")
                out[k] = "Dịch: " + "".join(ch for ch in t if not ("一" <= ch <= "鿿"))
            txt = json.dumps(out, ensure_ascii=False)
        resp = json.dumps({"choices": [{"message": {"content": "<think>…</think>" + txt}}]})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(resp.encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PROVIDERS["_mock"] = {"kind": "openai", "base_url": f"http://127.0.0.1:{srv.server_address[1]}", "models": ["m"]}
prov = make_provider("_mock", "k", "m")

G = {"林澈": "Lâm Triệt", "师尊": "sư tôn", "掌门": "chưởng môn"}
cues = [srt.Cue(1, "00:00:01,000", "00:00:02,000", "师尊，弟子来迟了。"),
        srt.Cue(2, "00:00:03,000", "00:00:04,000", "林澈，你可知罪？"),
        srt.Cue(3, "00:00:05,000", "00:00:06,000", "今日天气很好。")]

# ---- G1 chỉ gửi thuật ngữ có trong đoạn
rec("G1", "Chỉ gửi thuật ngữ xuất hiện trong đoạn", glossary_for("林澈来了", G) == {"林澈": "Lâm Triệt"},
    glossary_for("林澈来了", G))
rec("G2", "Phát hiện câu dịch sai thuật ngữ", glossary_miss("林澈来了", "Lin Che đến", G) == ["林澈"]
    and glossary_miss("林澈来了", "lâm triệt đến", G) == [], glossary_miss("林澈来了", "Lin Che đến", G))

# ---- G3 dịch có thuật ngữ: lần đầu model dùng sai -> vòng dịch bù sửa
LOG.clear(); MODE["obey_glossary_after"] = 1
tr = Translator([prov], Settings(threads=1, batch_size=10, glossary=G), threading.Event())
res = tr.translate_cues(cues)
first_user = LOG[0]["user"]
ok = ("THUẬT NGỮ BẮT BUỘC" in first_user and "林澈 = Lâm Triệt" in first_user and "掌门" not in first_user
      and "Lâm Triệt" in res[2] and "sư tôn" in res[1])
rec("G3", "Gửi kèm thuật ngữ; câu dùng sai được dịch bù đúng", ok,
    f"số lần gọi={len(LOG)}, kết quả={res}")

# ---- G4 dịch kỹ: thêm lượt biên tập, kết quả cuối là bản đã biên tập
LOG.clear(); MODE["obey_glossary_after"] = 0
tr = Translator([prov], Settings(threads=1, batch_size=10, glossary=G, refine=True), threading.Event())
res = tr.translate_cues(cues)
kinds = ["biên tập" if "biên tập viên" in x["system"] else "dịch" for x in LOG]
refine_in = json.loads(LOG[1]["user"][LOG[1]["user"].find("{"):]) if len(LOG) > 1 else {}
ok = kinds == ["dịch", "biên tập"] and all(v.endswith("(đã biên tập)") for v in res.values()) \
    and refine_in.get("2", {}).get("goc") == "林澈，你可知罪？"
rec("G4", "Dịch kỹ: 1 lượt dịch + 1 lượt biên tập (gửi cả câu gốc và bản nháp)", ok, f"các lượt={kinds}, kết quả={res}")

# ---- G5 không bật gì: chỉ 1 lượt, không có khối thuật ngữ
LOG.clear()
Translator([prov], Settings(threads=1, batch_size=10), threading.Event()).translate_cues(cues)
rec("G5", "Không bật thuật ngữ/dịch kỹ: giữ nguyên như cũ (1 lượt, không khối thuật ngữ)",
    len(LOG) == 1 and "THUẬT NGỮ" not in LOG[0]["user"], f"số lần gọi={len(LOG)}")

# ---- G6 AI lập bảng thuật ngữ
LOG.clear()
terms = extract_glossary([prov], [c.text for c in cues] * 3, "Tiếng Trung", log=lambda *_: None)
rec("G6", "AI lập bảng: bỏ từ bịa, bỏ trùng, tên dài xếp trước", [t["src"] for t in terms] == ["林澈", "师尊"],
    terms)

# ---- G7 hộp thoại: AI lập -> lưu file -> tab dịch đọc lại
from PySide6.QtWidgets import QApplication  # noqa: E402
from app import theme  # noqa: E402
from app.glossary_dialog import GlossaryDialog, glossary_path, read_terms  # noqa: E402
app = QApplication.instance() or QApplication(sys.argv)
app.setStyleSheet(theme.stylesheet())
d = tempfile.mkdtemp()
f = os.path.join(d, "tap1.zh.srt")
Path(f).write_text(srt.compose(cues), encoding="utf-8")
dlg = GlossaryDialog(glossary_path(d), [f], lambda: [prov], "Tiếng Trung", "Tiếng Việt")
dlg.show()
dlg.btn_ai.click()
t0 = time.time()
while (dlg.job is None or dlg.job.isRunning() or dlg.tb.rowCount() == 0) and time.time() - t0 < 20:
    app.processEvents(); time.sleep(0.01)
app.processEvents()
dlg.tb.item(0, 1).setText("Lâm Triệt (sửa tay)")
dlg.grab().save(os.path.join(Path(__file__).parent, "qa_data", "bang_thuat_ngu.png"))
dlg._save()
saved = read_terms(glossary_path(d))
rec("G7", "Hộp thoại: AI lập, sửa tay, lưu thuat_ngu.json", [t["vi"] for t in saved] == ["Lâm Triệt (sửa tay)", "sư tôn"],
    saved)

print(f"\n{sum(RES)}/{len(RES)} PASS")
sys.exit(0 if all(RES) else 1)
