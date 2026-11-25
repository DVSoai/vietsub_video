"""Bilibili: tìm kiếm video, lấy danh sách tập, tải về bằng yt-dlp (không cần đăng nhập).

- Tìm kiếm: /x/web-interface/search/type (chỉ cần cookie buvid3 mà Bilibili cấp cho mọi khách).
  Nếu bị chặn (-412/-352) thì thử lại bản có chữ ký WBI.
- Danh sách tập: /x/web-interface/view (video nhiều phần “P1, P2…” và tuyển tập ugc_season).
- Tải: yt-dlp + ffmpeg đi kèm app (ghép hình + tiếng thành .mp4).
  Khách chưa đăng nhập thường chỉ được chất lượng thấp (khoảng 360p–480p).
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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

ORDERS = {"Liên quan nhất": "totalrank", "Xem nhiều nhất": "click", "Mới đăng": "pubdate",
          "Nhiều bình luận chạy": "dm", "Nhiều lượt lưu": "stow"}
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

    def __init__(self, api: str = API, web: str = WEB):
        self.api, self.web = api.rstrip("/"), web.rstrip("/")
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
    "Chỉ âm thanh": "ba/b",
}


@dataclass
class DownloadJob:
    url: str
    title: str
    out_dir: str
    quality: str = "Cao nhất có thể"
    state: str = "queued"          # queued | running | done | error | stopped
    pct: float = 0.0
    info: str = ""
    file: str = ""
    _phase: dict = field(default_factory=dict)


class Downloader:
    """Tải tuần tự từng job bằng yt-dlp; gọi on_update(job) mỗi khi có thay đổi."""

    def __init__(self, on_update=lambda job: None, log=print):
        self.on_update, self.log = on_update, log
        self.stop_event = threading.Event()

    def _ffmpeg(self) -> str | None:
        try:
            from .transcribe import ffmpeg_exe
            return ffmpeg_exe()
        except Exception:  # noqa: BLE001
            return None

    def run(self, job: DownloadJob):
        try:
            import yt_dlp
        except ImportError:
            job.state, job.info = "error", "Thiếu yt-dlp: chạy  pip install yt-dlp"
            self.on_update(job)
            return
        os.makedirs(job.out_dir, exist_ok=True)
        job.state, job.info, job.pct = "running", "Đang lấy thông tin…", 0.0
        self.on_update(job)
        last = [0.0]

        def hook(d):
            if self.stop_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Đã dừng")
            st = d.get("status")
            fn = d.get("filename", "")
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
        name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", job.title).strip()[:90] or "%(title).90B"
        name = name.replace("%", "%%") if name != "%(title).90B" else name
        opts = {
            "format": QUALITIES.get(job.quality, "bv*+ba/b"),
            "outtmpl": os.path.join(job.out_dir, name + " [%(id)s].%(ext)s"),
            "merge_output_format": "mp4" if job.quality != "Chỉ âm thanh" else None,
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
        opts = {k: v for k, v in opts.items() if v is not None}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=True)
                path = None
                for r in (info or {}).get("requested_downloads") or []:
                    path = r.get("filepath") or path
                job.file = path or ydl.prepare_filename(info)
            job.state, job.pct = "done", 100.0
            h = (info or {}).get("height")
            job.info = f"Xong · {os.path.basename(job.file)}" + (f" · {h}p" if h else "")
        except Exception as e:  # noqa: BLE001
            if self.stop_event.is_set():
                job.state, job.info = "stopped", "Đã dừng"
            else:
                msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
                job.state, job.info = "error", msg[:200]
        self.on_update(job)
