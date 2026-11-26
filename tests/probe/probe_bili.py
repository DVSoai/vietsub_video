"""Gọi thật các API danh sách của Bilibili để chọn API cho tính năng Duyệt theo mục (chạy trên GitHub Actions)."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.bilibili import Bili

b = Bili()

def show(name, path, params, sign=False):
    try:
        p = b._sign(params) if sign else params
        d = b._get(path, p)
    except Exception as e:  # noqa: BLE001
        print(f"## {name}: EXC {e}"); return
    data = d.get("data")
    lst = None
    if isinstance(data, dict):
        for k in ("list", "archives", "item", "result"):
            if isinstance(data.get(k), list):
                lst = data[k]; break
    elif isinstance(data, list):
        lst = data
    n = len(lst) if lst is not None else None
    print(f"## {name}: code={d.get('code')} msg={d.get('message')!r} items={n}")
    if lst:
        it = lst[0]
        keys = sorted(it.keys()) if isinstance(it, dict) else type(it)
        print("   keys:", keys)
        if isinstance(it, dict):
            print("   first:", json.dumps({k: it.get(k) for k in ("bvid", "title", "pic", "cover", "duration", "pubdate",
                  "tid", "tname", "tid_v2", "tname_v2", "url", "season_id")}, ensure_ascii=False)[:400])
            print("   owner/stat:", json.dumps({"owner": it.get("owner"), "stat": (it.get("stat") or {}).get("view")},
                  ensure_ascii=False)[:200])
            print("   titles:", [x.get("title", "")[:20] for x in lst[:5] if isinstance(x, dict)])
    elif isinstance(data, dict):
        print("   data keys:", sorted(data.keys())[:20])
    time.sleep(0.6)

show("popular", "/x/web-interface/popular", {"pn": 1, "ps": 20})
show("popular wbi", "/x/web-interface/popular", {"pn": 1, "ps": 20, "web_location": "333.934"}, sign=True)
for rid in (0, 1001, 1021, 2005, 1005, 1002, 1010, 1020, 181, 85):
    show(f"ranking/v2 rid={rid}", "/x/web-interface/ranking/v2", {"rid": rid, "type": "all"})
for rid in (1001, 1021):
    show(f"ranking/v2 wbi rid={rid}", "/x/web-interface/ranking/v2", {"rid": rid, "type": "all", "web_location": "333.934"}, sign=True)
for rid in (181, 85, 1001, 1021, 2005):
    show(f"dynamic/region rid={rid}", "/x/web-interface/dynamic/region", {"rid": rid, "pn": 1, "ps": 20})
for rid in (85, 1021):
    show(f"newlist rid={rid}", "/x/web-interface/newlist", {"rid": rid, "pn": 1, "ps": 20, "type": 0})
show("region/feed/rcmd 1021", "/x/web-interface/region/feed/rcmd", {"display_id": 1, "request_cnt": 15, "from_region": 1021, "device": "web", "plat": 30})
show("playlet queryList", "/pgc/activity/rank/ugc/playlet/queryList", {})
s = b.search("短剧", 1)
print("## search baseline:", len(s.videos), [v.title[:15] for v in s.videos[:3]])
