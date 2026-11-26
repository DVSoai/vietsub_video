"""Gọi THẬT Bilibili cho mọi kiểu duyệt và mọi mục trong app (chạy trên GitHub Actions)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.bilibili import BROWSE_CATS, Bili

b = Bili(); res = []
def rec(name, fn):
    try:
        r = fn(); ok = len(r.videos) > 0 and all(v.bvid and v.title and v.cover.startswith("https://") for v in r.videos)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(r.videos)} video, {r.num_pages} trang · "
              f"{' | '.join(v.title[:14] for v in r.videos[:3])}", flush=True)
    except Exception as e:  # noqa: BLE001
        ok = False; print(f"[FAIL] {name}: {e}", flush=True)
    res.append(ok); time.sleep(0.5)

rec("Thịnh hành trang 1", lambda: b.popular(1))
rec("Thịnh hành trang 2", lambda: b.popular(2))
for cat, rid in BROWSE_CATS.items():
    rec(f"Xếp hạng · {cat} ({rid})", lambda rid=rid: b.ranking(rid, 1))
for cat, rid in BROWSE_CATS.items():
    if rid:
        rec(f"Đề xuất · {cat} ({rid})", lambda rid=rid: b.region_rcmd(rid, 1))
r1 = b.region_rcmd(1021, 1); r2 = b.region_rcmd(1021, 2)
diff = len({v.bvid for v in r2.videos} - {v.bvid for v in r1.videos})
print(f"[{'PASS' if diff else 'FAIL'}] Đề xuất · Trang sau ra video khác: {diff}/{len(r2.videos)} video mới", flush=True)
res.append(bool(diff))
v = b.ranking(1001, 1).videos[0]
t, parts = b.parts(v.bvid)
print(f"[{'PASS' if parts else 'FAIL'}] Video lấy từ Xếp hạng mở được danh sách tập để tải: {t[:20]} · {len(parts)} phần", flush=True)
res.append(bool(parts))
print(f"\n{sum(res)}/{len(res)} PASS", flush=True)
