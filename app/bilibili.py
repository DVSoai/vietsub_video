"""Bilibili: tìm kiếm video, lấy danh sách tập, tải về bằng yt-dlp (không cần đăng nhập).

- Tìm kiếm: /x/web-interface/search/type (chỉ cần cookie buvid3 mà Bilibili cấp cho mọi khách).
  Nếu bị chặn (-412/-352) thì thử lại bản có chữ ký WBI.
- Danh sách tập: /x/web-interface/view (video nhiều phần “P1, P2…” và tuyển tập ugc_season).
- Tải: yt-dlp + ffmpeg đi kèm app (ghép hình + tiếng thành .mp4).
  Gửi kèm try_look=1 nên khách chưa đăng nhập vẫn lấy được tới 1080p (1080P+, 4K, 60fps cần VIP).
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

import requests

API = "https://api.bilibili.com"
WEB = "https://www.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
# cookie đăng nhập cần giữ lại (SESSDATA là chính; bili_jct dùng cho thao tác ghi, app không cần)
LOGIN_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

ORDERS = {"Liên quan nhất": "totalrank", "Xem nhiều nhất": "click", "Mới đăng": "pubdate",
          "Nhiều bình luận chạy": "dm", "Nhiều lượt lưu": "stow"}
# Duyệt không cần từ khóa (đã gọi thử thật 26/11/2026: popular, ranking/v2 với mã mục mới, region/feed/rcmd đều chạy;
# dynamic/region và newlist không còn trả video). Mã mục là tid_v2 (hệ phân mục mới của Bilibili).
BROWSE_MODES = {"🔥 Thịnh hành hôm nay": "popular", "🏆 Bảng xếp hạng": "rank", "✨ Đề xuất trong mục": "rcmd"}
BROWSE_CATS = {
    "Toàn trang": 0,
    "Phim ảnh · review, cắt phim (影视)": 1001,
    "Tiểu kịch trường · phim ngắn tự diễn (小剧场)": 1021,
    "Hoạt hình (动画)": 1005,
    "Giải trí · sao, show (娱乐)": 1002,
    "Hài · chế (鬼畜)": 1007,
    "Âm nhạc (音乐)": 1003,
    "Nhảy (舞蹈)": 1004,
    "Game (游戏)": 1008,
    "Kiến thức (知识)": 1010,
    "Công nghệ (科技数码)": 1012,
    "Tin tức (资讯)": 1009,
    "Ẩm thực (美食)": 1020,
    "Động vật (动物)": 1024,
    "Thể thao (体育运动)": 1018,
    "Ô tô (汽车)": 1013,
    "Thời trang · làm đẹp (时尚美妆)": 1014,
    "Du lịch (出行)": 1022,
    "Vlog": 1029,
}
BROWSE_PAGE = 20

DURATIONS = {"Mọi thời lượng": 0, "Dưới 10 phút": 1, "10–30 phút": 2, "30–60 phút": 3, "Trên 60 phút": 4}

_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42,
          19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51,
          30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


class BiliError(Exception):
    pass


@dataclass
class Video:
    bvid: str
    title: str            # tiêu đề gốc (đã bỏ thẻ <em>)
    author: str
    cover: str            # url ảnh bìa
    duration: str         # "4:46"
    play: int
    pubdate: int
    desc: str = ""
    title_vi: str = ""    # tiêu đề đã dịch (nếu có)

    @property
    def url(self) -> str:
        return f"{WEB}/video/{self.bvid}"


@dataclass
class Part:
    url: str
    title: str
    duration: int = 0     # giây
    index: int = 0


@dataclass
class SearchResult:
    videos: list[Video]
    page: int
    num_pages: int
    total: int
    note: str = ""        # ghi chú cho người dùng (vd. mục không có bảng xếp hạng nên hiện Đề xuất)


@dataclass
class SubTrack:
    lan: str              # "zh-CN", "ai-zh", "en-US"…
    name: str             # "中文（中国）"
    url: str
    ai: bool = False      # phụ đề do Bilibili tự tạo bằng AI

    @property
    def label(self) -> str:
        return f"{self.name} ({self.lan})" + (" · Tự động (AI)" if self.ai else " · Do người đăng làm")

    @property
    def code(self) -> str:
        """Mã ngôn ngữ ngắn cho tên file: ai-zh / zh-CN / zh-Hans -> zh."""
        lan = self.lan[3:] if self.lan.startswith("ai-") else self.lan
        return (lan.split("-")[0] or "sub").lower()


def _ts(sec, sep: str = ",") -> str:
    ms = max(0, int(round(float(sec) * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def subs_to_srt(body: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(c.get('from', 0))} --> {_ts(c.get('to', 0))}\n{(c.get('content') or '').strip()}\n"
                     for i, c in enumerate(body, 1))


def subs_to_vtt(body: list[dict]) -> str:
    return "WEBVTT\n\n" + "\n".join(
        f"{_ts(c.get('from', 0), '.')} --> {_ts(c.get('to', 0), '.')}\n{(c.get('content') or '').strip()}\n" for c in body)


def subs_to_txt(body: list[dict]) -> str:
    return "\n".join((c.get("content") or "").strip() for c in body) + "\n"


def best_tracks(tracks: list[SubTrack]) -> dict[str, SubTrack]:
    """Mỗi ngôn ngữ một bản: ưu tiên bản người đăng làm hơn bản AI."""
    out: dict[str, SubTrack] = {}
    for t in sorted(tracks, key=lambda t: t.ai):
        out.setdefault(t.code, t)
    return out


def fmt_duration(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return str(sec or "")
    h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def video_from_item(it: dict) -> "Video | None":
    """Một mục trong danh sách thịnh hành / xếp hạng / đề xuất -> Video (bỏ qua mục không phải video thường)."""
    bvid = it.get("bvid")
    if not bvid or it.get("goto") not in (None, "av"):
        return None
    pic = it.get("pic") or it.get("cover") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    elif pic.startswith("http://"):
        pic = "https://" + pic[7:]
    owner = it.get("owner") or {}
    author = owner.get("name") if isinstance(owner, dict) else ""
    if not author:
        a = it.get("author")
        author = a.get("name", "") if isinstance(a, dict) else (a or "")
    stat = it.get("stat") or {}
    return Video(bvid=bvid, title=clean_title(it.get("title")), author=author or "", cover=pic,
                 duration=fmt_duration(it.get("duration")), play=int(stat.get("view") or stat.get("play") or 0),
                 pubdate=int(it.get("pubdate") or 0), desc=clean_title(it.get("desc") or ""))


def clean_title(t: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", t or "")).strip()


def fmt_count(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f} tỷ"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} tr"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class Bili:
    """Phiên làm việc với Bilibili (giữ cookie, khóa WBI)."""

    def __init__(self, api: str = API, web: str = WEB, passport: str = PASSPORT):
        self.api, self.web, self.passport = api.rstrip("/"), web.rstrip("/"), passport.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": self.web + "/", "Origin": self.web})
        self._wbi: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._ready = False

    # ---------------------------------------------------------- cookie khách
    def _ensure_cookie(self):
        with self._lock:
            if self._ready:
                return
            try:
                self.s.get(self.web + "/", timeout=15)
            except requests.RequestException:
                pass
            if not self.s.cookies.get("buvid3"):
                try:
                    d = self.s.get(self.api + "/x/frontend/finger/spi", timeout=15).json().get("data", {})
                    if d.get("b_3"):
                        self.s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
                    if d.get("b_4"):
                        self.s.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
                except (requests.RequestException, ValueError):
                    pass
            self._ready = True

    def _get(self, path: str, params: dict) -> dict:
        self._ensure_cookie()
        try:
            r = self.s.get(self.api + path, params=params, timeout=20)
            data = r.json()
        except requests.RequestException as e:
            raise BiliError(f"Không kết nối được Bilibili: {e}") from e
        except ValueError:
            raise BiliError(f"Bilibili trả về dữ liệu lạ (HTTP {r.status_code})")
        return data

    # ---------------------------------------------------------- chữ ký WBI (dự phòng)
    def _mixin_key(self) -> str:
        if self._wbi and time.time() - self._wbi[1] < 3600:
            return self._wbi[0]
        d = self._get("/x/web-interface/nav", {}).get("data", {}).get("wbi_img", {})
        img = os.path.splitext(os.path.basename(d.get("img_url", "")))[0]
        sub = os.path.splitext(os.path.basename(d.get("sub_url", "")))[0]
        if not img or not sub:
            raise BiliError("Không lấy được khóa WBI")
        key = "".join((img + sub)[i] for i in _MIXIN)[:32]
        self._wbi = (key, time.time())
        return key

    def _sign(self, params: dict) -> dict:
        p = dict(params, wts=int(time.time()))
        p = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(p.items())}
        q = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
        p["w_rid"] = hashlib.md5((q + self._mixin_key()).encode()).hexdigest()
        return p

    # ---------------------------------------------------------- tìm kiếm
    def search(self, keyword: str, page: int = 1, order: str = "totalrank", duration: int = 0) -> SearchResult:
        params = {"search_type": "video", "keyword": keyword, "page": page,
                  "order": order, "duration": duration}
        data = self._get("/x/web-interface/search/type", params)
        if data.get("code") in (-412, -352, -403):
            data = self._get("/x/web-interface/wbi/search/type", self._sign(params))
        if data.get("code") != 0:
            raise BiliError(f"Bilibili từ chối tìm kiếm (mã {data.get('code')}: {data.get('message')})")
        d = data.get("data") or {}
        vids = []
        for it in d.get("result") or []:
            if it.get("type") != "video" or not it.get("bvid"):
                continue
            pic = it.get("pic") or ""
            vids.append(Video(
                bvid=it["bvid"], title=clean_title(it.get("title")), author=it.get("author") or "",
                cover=("https:" + pic) if pic.startswith("//") else pic,
                duration=str(it.get("duration") or ""), play=int(it.get("play") or 0),
                pubdate=int(it.get("pubdate") or 0), desc=clean_title(it.get("description") or "")))
        return SearchResult(vids, int(d.get("page") or page), int(d.get("numPages") or 1),
                            int(d.get("numResults") or len(vids)))

    # ---------------------------------------------------------- duyệt không cần từ khóa
    def _ok(self, data: dict, what: str) -> dict:
        if data.get("code") != 0:
            raise BiliError(f"Bilibili từ chối {what} (mã {data.get('code')}: {data.get('message')})")
        return data.get("data") or {}

    @staticmethod
    def _videos(items) -> list:
        out, seen = [], set()
        for it in items or []:
            v = video_from_item(it) if isinstance(it, dict) else None
            if v and v.bvid not in seen:
                seen.add(v.bvid); out.append(v)
        return out

    def popular(self, page: int = 1) -> SearchResult:
        """Thịnh hành hôm nay (热门), 20 video mỗi trang."""
        d = self._ok(self._get("/x/web-interface/popular", {"pn": page, "ps": BROWSE_PAGE}), "trang thịnh hành")
        vids = self._videos(d.get("list"))
        more = not d.get("no_more") and bool(vids)
        return SearchResult(vids, page, page + 1 if more else page, len(vids))

    def ranking(self, rid: int = 0, page: int = 1) -> SearchResult:
        """Bảng xếp hạng 3 ngày của một mục (tối đa 100 video, chia 20 video mỗi trang)."""
        key = ("rank", rid)
        cache = getattr(self, "_rank_cache", {})
        self._rank_cache = cache
        hit = cache.get(key)
        if not hit or time.time() - hit[0] > 600:
            params = {"rid": rid, "type": "all"}
            data = self._get("/x/web-interface/ranking/v2", params)
            if data.get("code") in (-412, -352, -403):
                data = self._get("/x/web-interface/ranking/v2", self._sign(dict(params, web_location="333.934")))
            hit = (time.time(), self._videos(self._ok(data, "bảng xếp hạng").get("list")))
            cache[key] = hit
        allv = hit[1]
        pages = max(1, (len(allv) + BROWSE_PAGE - 1) // BROWSE_PAGE)
        page = max(1, min(page, pages))
        return SearchResult(allv[(page - 1) * BROWSE_PAGE: page * BROWSE_PAGE], page, pages, len(allv))

    def region_rcmd(self, rid: int, page: int = 1) -> SearchResult:
        """Đề xuất trong một mục: mỗi lần gọi một loạt video mới (trang sau = loạt kế tiếp)."""
        if not rid:
            return self.popular(page)
        params = {"display_id": page, "request_cnt": BROWSE_PAGE, "from_region": rid, "device": "web", "plat": 30}
        d = self._ok(self._get("/x/web-interface/region/feed/rcmd", params), "đề xuất theo mục")
        vids = self._videos(d.get("archives") or d.get("item") or d.get("list"))
        return SearchResult(vids, page, min(page + 1, 30) if vids else page, len(vids))

    def browse(self, mode: str, rid: int = 0, page: int = 1) -> SearchResult:
        if mode == "popular":
            return self.popular(page)
        if mode == "rank":
            try:
                return self.ranking(rid, page)
            except BiliError as e:
                if "-400" not in str(e) or not rid:
                    raise
                r = self.region_rcmd(rid, page)     # vd. 资讯 (1009): Bilibili không có bảng xếp hạng
                r.note = "mục này không có bảng xếp hạng, đang hiện Đề xuất"
                return r
        return self.region_rcmd(rid, page)

    # ---------------------------------------------------------- danh sách tập
    def parts(self, bvid: str) -> tuple[str, list[Part]]:
        """(tên chung, các phần). Video thường -> 1 phần; nhiều P -> mỗi P một phần;
        thuộc tuyển tập -> các tập trong tuyển tập."""
        data = self._get("/x/web-interface/view", {"bvid": bvid})
        if data.get("code") != 0:
            raise BiliError(f"Không lấy được thông tin video (mã {data.get('code')}: {data.get('message')})")
        d = data["data"]
        title = d.get("title", bvid)
        pages = d.get("pages") or []
        if len(pages) > 1:
            return title, [Part(f"{self.web}/video/{bvid}?p={p['page']}",
                                f"P{p['page']} · {p.get('part') or title}", int(p.get('duration') or 0), p["page"])
                           for p in pages]
        season = d.get("ugc_season") or {}
        eps = [e for s in season.get("sections") or [] for e in s.get("episodes") or []]
        if len(eps) > 1:
            out = []
            for i, e in enumerate(eps, 1):
                dur = int((e.get("arc") or {}).get("duration") or (e.get("page") or {}).get("duration") or 0)
                out.append(Part(f"{self.web}/video/{e['bvid']}", f"{i:02d} · {e.get('title') or e['bvid']}", dur, i))
            return season.get("title") or title, out
        return title, [Part(f"{self.web}/video/{bvid}", title, int(d.get("duration") or 0), 1)]

    # ---------------------------------------------------------- dán link
    def resolve(self, text: str) -> tuple[str, int]:
        """Link thường / b23.tv / BV… / av… -> (bvid, số phần p; 0 = không chỉ định)."""
        t = text.strip()
        m = re.search(r"(https?://)?(b23\.tv|bili2233\.cn)/[\w-]+", t)
        if m:
            short = m.group(0) if m.group(1) else "https://" + m.group(0)
            try:
                r = self.s.get(short, timeout=15, allow_redirects=True)
                t = r.url
            except requests.RequestException as e:
                raise BiliError(f"Không mở được link rút gọn: {e}") from e
        p = 0
        mp = re.search(r"[?&]p=(\d+)", t)
        if mp:
            p = int(mp.group(1))
        mb = re.search(r"BV[0-9A-Za-z]{10}", t)
        if mb:
            return mb.group(0), p
        ma = re.search(r"\bav(\d+)", t, re.I)
        if ma:
            data = self._get("/x/web-interface/view", {"aid": ma.group(1)})
            if data.get("code") == 0:
                return data["data"]["bvid"], p
        raise BiliError("Không nhận ra link Bilibili. Dán link video, link b23.tv, mã BV… hoặc av…")

    def video_meta(self, bvid: str) -> dict:
        data = self._get("/x/web-interface/view", {"bvid": bvid})
        if data.get("code") != 0:
            raise BiliError(f"Không lấy được thông tin video (mã {data.get('code')}: {data.get('message')})")
        return data["data"]

    def streams(self, bvid: str, cid: int, max_h: int = 4320) -> dict:
        """Chọn luồng hình (ưu tiên H.264) + luồng tiếng tốt nhất mà khách được xem."""
        # try_look=1: khách chưa đăng nhập vẫn được luồng 720p/1080p (chế độ “xem thử” của trình phát)
        params = {"bvid": bvid, "cid": cid, "qn": 127, "fnval": 4048, "fnver": 0, "fourk": 1, "try_look": 1}
        data = self._get("/x/player/playurl", params)
        if data.get("code") in (-412, -352, -403):
            data = self._get("/x/player/wbi/playurl", self._sign(params))
        if data.get("code") != 0:
            raise BiliError(f"Bilibili không cho lấy luồng phát (mã {data.get('code')}: {data.get('message')})")
        d = data.get("data") or {}
        dash = d.get("dash")
        if not dash:
            durl = d.get("durl") or []
            if not durl:
                raise BiliError("Video không có luồng phát cho khách (có thể cần đăng nhập/VIP)")
            u = durl[0]
            return {"single": [u.get("url")] + (u.get("backup_url") or []), "height": 0}

        def urls(x):
            return [u for u in [x.get("baseUrl") or x.get("base_url")] + (x.get("backupUrl") or x.get("backup_url") or []) if u]
        vids = [v for v in dash.get("video") or [] if urls(v)]
        ok = [v for v in vids if (v.get("height") or 0) <= max_h] or vids
        if not ok:
            raise BiliError("Không có luồng hình nào")
        best_h = max(v.get("height") or 0 for v in ok)
        same = [v for v in ok if (v.get("height") or 0) == best_h]
        avc = [v for v in same if str(v.get("codecs", "")).startswith("avc")]
        v = max(avc or same, key=lambda x: x.get("bandwidth") or 0)
        auds = [a for a in (dash.get("audio") or []) if urls(a)]
        flac = ((dash.get("flac") or {}).get("audio"))
        if flac and urls(flac):
            auds.append(flac)
        a = max(auds, key=lambda x: x.get("bandwidth") or 0) if auds else None
        return {"video": urls(v), "audio": urls(a) if a else [], "height": best_h,
                "codec": str(v.get("codecs", "")), "duration": int(dash.get("duration") or 0)}

    def cid_for(self, url: str) -> tuple[str, int, int, int]:
        """Link video (có thể có ?p=N) -> (bvid, cid, p, số phần)."""
        bvid = re.search(r"BV[0-9A-Za-z]{10}", url).group(0)
        mp = re.search(r"[?&]p=(\d+)", url)
        p = int(mp.group(1)) if mp else 1
        meta = self.video_meta(bvid)
        pages = meta.get("pages") or []
        page = next((x for x in pages if x.get("page") == p), pages[0] if pages else None)
        cid = (page or {}).get("cid") or meta.get("cid")
        if not cid:
            raise BiliError("Không tìm thấy cid của video")
        return bvid, int(cid), p, len(pages)

    # ---------------------------------------------------------- đăng nhập (quét mã QR bằng app Bilibili)
    def logged_in(self) -> bool:
        return bool(self.s.cookies.get("SESSDATA"))

    def login_cookies(self) -> dict:
        return {c.name: c.value for c in self.s.cookies if c.name in LOGIN_COOKIES}

    def _cookie_domain(self) -> str:
        host = urllib.parse.urlparse(self.web).hostname or "bilibili.com"
        return ".bilibili.com" if host.endswith("bilibili.com") else host   # host khác: máy chủ giả lập khi kiểm thử

    def set_login(self, cookies: dict):
        for k, v in (cookies or {}).items():
            if k in LOGIN_COOKIES and v:
                self.s.cookies.set(k, v, domain=self._cookie_domain())
        self._wbi = None

    def logout(self):
        for c in list(self.s.cookies):
            if c.name in LOGIN_COOKIES:
                self.s.cookies.clear(c.domain, c.path, c.name)
        self._wbi = None

    def account(self) -> dict:
        """{'login': bool, 'name': str, 'vip': bool}"""
        d = self._get("/x/web-interface/nav", {}).get("data") or {}
        return {"login": bool(d.get("isLogin")), "name": d.get("uname") or "",
                "vip": bool(d.get("vipStatus"))}

    def qr_start(self) -> tuple[str, str]:
        """(nội dung mã QR, qrcode_key)"""
        try:
            d = self.s.get(self.passport + "/x/passport-login/web/qrcode/generate", timeout=15).json()
        except (requests.RequestException, ValueError) as e:
            raise BiliError(f"Không lấy được mã QR đăng nhập: {e}") from e
        if d.get("code") != 0:
            raise BiliError(f"Không lấy được mã QR (mã {d.get('code')}: {d.get('message')})")
        return d["data"]["url"], d["data"]["qrcode_key"]

    def qr_poll(self, key: str) -> str:
        """'wait' | 'scanned' (đã quét, chờ bấm xác nhận) | 'expired' | 'ok' (cookie đã vào phiên)."""
        try:
            d = self.s.get(self.passport + "/x/passport-login/web/qrcode/poll",
                           params={"qrcode_key": key}, timeout=15).json()
        except (requests.RequestException, ValueError):
            return "wait"
        code = (d.get("data") or {}).get("code", d.get("code"))
        if code == 0:
            # cookie thường được đặt qua Set-Cookie; một số trường hợp chỉ có trong data.url
            url = (d.get("data") or {}).get("url") or ""
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.set_login({k: v[0] for k, v in q.items() if k in LOGIN_COOKIES})
            for c in list(self.s.cookies):          # chuyển cookie của passport sang toàn miền
                if c.name in LOGIN_COOKIES and c.domain != self._cookie_domain():
                    self.s.cookies.clear(c.domain, c.path, c.name)
                    self.s.cookies.set(c.name, c.value, domain=self._cookie_domain())
            self._wbi = None
            return "ok" if self.logged_in() else "wait"
        return {86090: "scanned", 86038: "expired"}.get(code, "wait")

    # ---------------------------------------------------------- phụ đề có sẵn (CC / AI)
    def subtitles(self, bvid: str, cid: int) -> list[SubTrack]:
        data = self._get("/x/player/wbi/v2", self._sign({"bvid": bvid, "cid": cid}))
        if data.get("code") != 0:
            data = self._get("/x/player/v2", {"bvid": bvid, "cid": cid})
        if data.get("code") != 0:
            raise BiliError(f"Không lấy được phụ đề (mã {data.get('code')}: {data.get('message')})")
        d = data.get("data") or {}
        tracks = []
        for s in (d.get("subtitle") or {}).get("subtitles") or []:
            u = s.get("subtitle_url") or ""
            if not u:
                continue
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("http://"):
                u = "https://" + u[7:]
            lan = s.get("lan") or "sub"
            tracks.append(SubTrack(lan, s.get("lan_doc") or lan, u, ai=s.get("type") == 1 or lan.startswith("ai-")))
        if not tracks and not self.logged_in() and d.get("need_login_subtitle", True):
            raise BiliError("Bilibili chỉ đưa phụ đề cho tài khoản đã đăng nhập. "
                            "Bấm “Đăng nhập Bilibili” (quét mã QR bằng app Bilibili) rồi thử lại.")
        return tracks

    def subtitle_body(self, track: SubTrack) -> list[dict]:
        """Danh sách câu [{'from': giây, 'to': giây, 'content': chữ}, …]"""
        try:
            r = self.s.get(track.url, timeout=20)
            body = r.json().get("body")
        except (requests.RequestException, ValueError) as e:
            raise BiliError(f"Không tải được phụ đề: {e}") from e
        if not isinstance(body, list):
            raise BiliError("File phụ đề của Bilibili không đúng định dạng")
        return body

    def image(self, url: str) -> bytes:
        """Ảnh bìa (bản thu nhỏ của máy chủ ảnh Bilibili cho nhẹ)."""
        if "hdslb.com" in url and "@" not in url:
            try:
                r = self.s.get(url + "@320w_180h_1c.jpg", timeout=20)
                if r.status_code == 200 and r.content:
                    return r.content
            except requests.RequestException:
                pass
        r = self.s.get(url, timeout=20)
        r.raise_for_status()
        return r.content


# ====================================================================== tải về
QUALITIES = {
    "Cao nhất có thể": "bv*+ba/b",
    "Tối đa 720p": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    "Tối đa 480p": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    "Chỉ âm thanh (.m4a)": "ba/b",
}
MAX_H = {"Cao nhất có thể": 4320, "Tối đa 720p": 720, "Tối đa 480p": 480, "Chỉ âm thanh (.m4a)": 4320}


@dataclass
class DownloadJob:
    url: str
    title: str
    out_dir: str
    quality: str = "Cao nhất có thể"
    cover: bool = False            # tải kèm ảnh bìa
    auto_title: bool = False       # chưa biết tên -> lấy tiêu đề gốc từ trang (trang ngoài Bilibili)
    cookies_browser: str = ""      # "firefox" | "edge" | "chrome" | "" (Douyin hay cần)
    subs: bool = False             # tải kèm phụ đề có sẵn của Bilibili (.zh.srt…)
    thumb_url: str = ""            # ảnh bìa để hiện trong hàng chờ
    state: str = "queued"          # queued | running | paused | done | error | stopped | removed
    pct: float = 0.0
    info: str = ""
    file: str = ""
    cancel: str = ""               # "pause" | "remove": yêu cầu dừng riêng job này
    _phase: dict = field(default_factory=dict)
    temps: list = field(default_factory=list)   # file tạm (.part) để xoá khi bỏ job


class _Stopped(Exception):
    pass


def _safe_name(t: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]', " ", t).strip()[:90]


def remove_temps(job: DownloadJob):
    """Xoá phần đã tải dở của job (khi người dùng bỏ hẳn job)."""
    for t in job.temps:
        for x in (t, t + ".ytdl"):
            try:
                if os.path.exists(x):
                    os.remove(x)
            except OSError:
                pass
    job.temps = []


class Downloader:
    """Tải từng job. Link Bilibili: tải thẳng qua API phát video (dùng cookie khách của app);
    hỏng thì thử yt-dlp. Gọi on_update(job) mỗi khi có thay đổi."""

    def __init__(self, on_update=lambda job: None, log=print, bili: "Bili | None" = None):
        self.on_update, self.log = on_update, log
        self.stop_event = threading.Event()
        self.bili = bili

    # ------------------------------------------------------------ chung
    def _halt(self, job: DownloadJob) -> bool:
        return self.stop_event.is_set() or bool(job.cancel)

    def _stopped(self, job: DownloadJob):
        """Ghi trạng thái sau khi job bị dừng giữa chừng."""
        if job.cancel == "remove":
            job.state, job.info = "removed", "Đã bỏ"
            remove_temps(job)
        elif job.cancel == "pause":
            job.state, job.info = "paused", "Đã tạm dừng · bấm ▶ Tiếp tục để tải tiếp từ chỗ dừng"
        else:
            job.state, job.info = "stopped", "Đã dừng"
        job.cancel = ""

    def run(self, job: DownloadJob):
        if job.cancel:
            self._stopped(job)
            self.on_update(job)
            return
        os.makedirs(job.out_dir, exist_ok=True)
        job.state, job.info, job.pct, job._phase = "running", "Đang lấy thông tin…", 0.0, {}
        self.on_update(job)
        errs = []
        if self.bili and re.search(r"BV[0-9A-Za-z]{10}", job.url):
            try:
                self._run_native(job)
                self.on_update(job)
                return
            except _Stopped:
                self._stopped(job)
                self.on_update(job)
                return
            except Exception as e:  # noqa: BLE001
                if self._halt(job):
                    self._stopped(job)
                    self.on_update(job)
                    return
                errs.append(f"API: {e}")
                self.log(f"  ⚠ Tải qua API lỗi ({job.title}): {e} → thử yt-dlp")
                job.info, job.pct, job._phase = "Thử cách khác (yt-dlp)…", 0.0, {}
                self.on_update(job)
        self._run_ytdlp(job)
        if job.state == "error" and errs:
            job.info = " | ".join(errs + [f"yt-dlp: {job.info}"])
        self.on_update(job)

    # ------------------------------------------------------------ cách 1: API phát video
    def _http(self):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": WEB + "/", "Origin": WEB})
        if self.bili:
            s.cookies.update(self.bili.s.cookies)
        return s

    def _fetch(self, sess, urls: list[str], dst: str, job: DownloadJob, key: str):
        last = None
        for attempt in range(3):
            for u in urls:
                if self._halt(job):
                    raise _Stopped()
                try:
                    have = os.path.getsize(dst) if os.path.exists(dst) else 0
                    hdr = {"Range": f"bytes={have}-"} if have else {}
                    with sess.get(u, headers=hdr, stream=True, timeout=30) as r:
                        if r.status_code == 416:
                            return
                        if r.status_code not in (200, 206):
                            raise BiliError(f"HTTP {r.status_code}")
                        if r.status_code == 200:
                            have = 0
                        total = have + int(r.headers.get("content-length") or 0)
                        mode = "ab" if have and r.status_code == 206 else "wb"
                        done, t0, last_ui = have, time.monotonic(), 0.0
                        with open(dst, mode) as f:
                            for chunk in r.iter_content(1 << 18):
                                if self._halt(job):
                                    raise _Stopped()
                                f.write(chunk)
                                done += len(chunk)
                                job._phase[key] = (done, total)
                                now = time.monotonic()
                                if now - last_ui > 0.3:
                                    last_ui = now
                                    self._progress(job, key, (done - have) / max(0.001, now - t0))
                    if total and os.path.getsize(dst) < total:
                        raise BiliError("tải chưa đủ dữ liệu")
                    return
                except _Stopped:
                    raise
                except Exception as e:  # noqa: BLE001
                    last = e
            for _ in range(int(15 * (attempt + 1))):     # chờ 1.5 s, 3 s… nhưng vẫn nghe lệnh dừng
                if self._halt(job):
                    raise _Stopped()
                time.sleep(0.1)
        raise BiliError(f"Không tải được luồng {key}: {last}")

    def _progress(self, job, key, speed):
        tot = sum(t for _, t in job._phase.values() if t)
        got = sum(min(a, t) if t else a for a, t in job._phase.values())
        job.pct = min(99.0, got * 100 / tot) if tot else job.pct
        a, t = job._phase[key]
        eta = (t - a) / speed if speed > 0 and t else 0
        label = {"video": "hình", "audio": "tiếng"}.get(key, "")
        job.info = (f"Đang tải {label} " if label else "Đang tải ") + \
            f"{a / 1e6:.1f}/{t / 1e6:.1f} MB · {speed / 1e6:.2f} MB/s" + \
            (f" · còn {int(eta) // 60}:{int(eta) % 60:02d}" if eta else "")
        self.on_update(job)

    def _save_subs(self, job: DownloadJob, bvid: str, cid: int, base: str) -> str:
        """Lưu phụ đề có sẵn cạnh video: tên_video.zh.srt (tab Dịch phụ đề dùng luôn, không phải nghe lại).
        Trả về ghi chú ngắn cho dòng trạng thái; lỗi phụ đề không làm hỏng việc tải video."""
        try:
            tracks = best_tracks(self.bili.subtitles(bvid, cid))
            if not tracks:
                return " · video không có phụ đề"
            for code, t in tracks.items():
                text, path = subs_to_srt(self.bili.subtitle_body(t)), f"{base}.{code}.srt"
                try:
                    same = open(path, encoding="utf-8").read() == text
                except OSError:
                    same = False
                if not same:          # không ghi lại file y hệt: giữ thời gian sửa để bản dịch cũ vẫn được dùng lại
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
            return " · phụ đề: " + ", ".join(f"{c}{' (AI)' if t.ai else ''}" for c, t in tracks.items())
        except Exception as e:  # noqa: BLE001
            self.log(f"  ⚠ Không lấy được phụ đề ({job.title}): {e}")
            return " · không lấy được phụ đề" + (" (cần đăng nhập)" if not self.bili.logged_in() else "")

    def _run_native(self, job: DownloadJob):
        bvid, cid, p, n_pages = self.bili.cid_for(job.url)
        meta = self.bili.video_meta(bvid)
        audio_only = job.quality.startswith("Chỉ âm thanh")
        st = self.bili.streams(bvid, cid, MAX_H.get(job.quality, 4320))
        base = os.path.join(job.out_dir, f"{_safe_name(job.title) or bvid} [{bvid}" + (f"_p{p}" if n_pages > 1 else "") + "]")
        out = base + (".m4a" if audio_only else ".mp4")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            note = self._save_subs(job, bvid, cid, base) if job.subs else ""
            job.file, job.state, job.pct, job.info = out, "done", 100.0, f"Đã có sẵn · {os.path.basename(out)}{note}"
            return
        sess = self._http()
        tmp_v, tmp_a = base + ".video.part", base + ".audio.part"
        job.temps = [tmp_v, tmp_a]
        if "single" in st:
            self._fetch(sess, st["single"], tmp_v, job, "file")
            os.replace(tmp_v, out)
        else:
            if not audio_only:
                self._fetch(sess, st["video"], tmp_v, job, "video")
            if st["audio"]:
                self._fetch(sess, st["audio"], tmp_a, job, "audio")
            job.info = "Đang ghép hình và tiếng…"
            self.on_update(job)
            self._merge(tmp_v if not audio_only else None, tmp_a if st["audio"] else None, out)
            for t in (tmp_v, tmp_a):
                if os.path.exists(t):
                    os.remove(t)
        if job.cover and meta.get("pic"):
            try:
                with open(base + ".jpg", "wb") as f:
                    f.write(sess.get(meta["pic"], timeout=20).content)
            except Exception:  # noqa: BLE001
                pass
        note = ""
        if job.subs:
            job.info = "Đang lấy phụ đề…"
            self.on_update(job)
            note = self._save_subs(job, bvid, cid, base)
        job.file, job.state, job.pct, job.temps = out, "done", 100.0, []
        job.info = f"Xong · {os.path.basename(out)}" + \
            (f" · {st['height']}p" if st.get("height") and not audio_only else "") + note

    def _merge(self, video: str | None, audio: str | None, out: str):
        import subprocess
        import sys as _sys
        ff = self._ffmpeg()
        if not ff:
            raise BiliError("Không có ffmpeg để ghép hình và tiếng")
        args = [ff, "-hide_banner", "-loglevel", "error", "-y"]
        for x in (video, audio):
            if x:
                args += ["-i", x]
        args += ["-c", "copy"]
        if video and audio:
            args += ["-map", "0:v:0", "-map", "1:a:0"]
        args += ["-movflags", "+faststart", out]
        flags = 0x08000000 if _sys.platform == "win32" else 0
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=flags)
        if r.returncode != 0:
            raise BiliError("ffmpeg ghép lỗi: " + r.stderr.strip()[-300:])

    def _ffmpeg(self) -> str | None:
        try:
            from .transcribe import ffmpeg_exe
            return ffmpeg_exe()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ cách 2: yt-dlp
    def _run_ytdlp(self, job: DownloadJob):
        try:
            import yt_dlp
        except ImportError:
            job.state, job.info = "error", "Thiếu yt-dlp: chạy  pip install yt-dlp"
            return
        last = [0.0]
        if not re.search(r"(bilibili\.com|b23\.tv)", job.url):
            from .sources import expand
            full = expand(job.url)
            if full != job.url:
                self.log(f"  Link rút gọn → {full}")
                job.url = full

        def hook(d):
            if self._halt(job):
                raise yt_dlp.utils.DownloadCancelled("Đã dừng")
            st = d.get("status")
            fn = d.get("filename", "")
            tmp = d.get("tmpfilename") or (fn + ".part" if fn else "")
            if tmp and tmp not in job.temps:
                job.temps.append(tmp)
            if job.auto_title and (d.get("info_dict") or {}).get("title"):
                job.title, job.auto_title = d["info_dict"]["title"][:120], False
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                job._phase[fn] = (done, total)
                tot = sum(t for _, t in job._phase.values() if t)
                got = sum(min(a, t) if t else a for a, t in job._phase.values())
                job.pct = min(99.0, got * 100 / tot) if tot else job.pct
                spd = d.get("speed") or 0
                eta = d.get("eta")
                # Bilibili tách hình và tiếng thành 2 luồng riêng rồi ghép
                inf = d.get("info_dict") or {}
                kind = "tiếng" if inf.get("vcodec") == "none" else ("hình" if inf.get("acodec") == "none" else "")
                job.info = (f"Đang tải {kind} " if kind else "Đang tải ") + \
                    f"{got / 1e6:.1f}/{tot / 1e6:.1f} MB · {spd / 1e6:.2f} MB/s" + \
                    (f" · còn {int(eta) // 60}:{int(eta) % 60:02d}" if eta else "")
                now = time.monotonic()
                if now - last[0] > 0.3:
                    last[0] = now
                    self.on_update(job)
            elif st == "finished":
                job.info = "Đang ghép hình và tiếng…"
                self.on_update(job)

        # tên file = tên hiển thị trong app (đã dịch nếu có) + mã video -> mỗi tập một tên riêng
        if job.auto_title:
            name = "%(title).80B"
        else:
            name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", job.title).strip()[:90] or "%(title).80B"
            name = name.replace("%", "%%") if name != "%(title).80B" else name
        opts = {
            "format": QUALITIES.get(job.quality, "bv*+ba/b"),
            "outtmpl": os.path.join(job.out_dir, name + " [%(id)s].%(ext)s"),
            "merge_output_format": "mp4" if not job.quality.startswith("Chỉ âm thanh") else None,
            "noplaylist": True,
            "quiet": True, "no_warnings": True, "noprogress": True,
            "progress_hooks": [hook],
            "http_headers": {"User-Agent": UA, "Referer": WEB + "/"},
            "retries": 5, "fragment_retries": 5,
            "windowsfilenames": True,
            "overwrites": False,
        }
        ff = self._ffmpeg()
        if ff:
            opts["ffmpeg_location"] = ff
        cookie_file = None
        is_bili = bool(re.search(r"(bilibili\.com|b23\.tv)", job.url))
        if not is_bili:
            opts["http_headers"] = {"User-Agent": UA}
        if job.cookies_browser:
            opts["cookiesfrombrowser"] = (job.cookies_browser,)
        elif is_bili and self.bili and self.bili.s.cookies:
            import tempfile
            from http.cookiejar import MozillaCookieJar
            cookie_file = os.path.join(tempfile.gettempdir(), f"st_bili_cookies_{os.getpid()}.txt")
            jar = MozillaCookieJar(cookie_file)
            for c in self.bili.s.cookies:
                jar.set_cookie(c)
            jar.save(ignore_discard=True, ignore_expires=True)
            opts["cookiefile"] = cookie_file
        opts = {k: v for k, v in opts.items() if v is not None}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=True)
                path = None
                for r in (info or {}).get("requested_downloads") or []:
                    path = r.get("filepath") or path
                job.file = path or ydl.prepare_filename(info)
            job.state, job.pct, job.temps = "done", 100.0, []
            h = (info or {}).get("height")
            job.info = f"Xong · {os.path.basename(job.file)}" + (f" · {h}p" if h else "")
        except Exception as e:  # noqa: BLE001
            if self._halt(job):
                self._stopped(job)
            else:
                msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
                if re.search(r"cookie", msg, re.I) and not job.cookies_browser:
                    msg += "  → Trang này cần cookie: ở mục Cookie chọn trình duyệt bạn hay dùng (nên dùng Firefox) rồi tải lại."
                elif re.search(r"cookie|decrypt|DPAPI|App-Bound", msg, re.I) and job.cookies_browser == "chrome":
                    msg += "  → Chrome mới khóa cookie, hãy thử Firefox hoặc Edge."
                job.state, job.info = "error", msg
