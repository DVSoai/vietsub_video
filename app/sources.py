"""Các trang video được hỗ trợ ngoài Bilibili (video công khai, tải bằng yt-dlp).

Không có YouTube và các trang phim VIP có mã hóa (Youku, iQiyi, Tencent Video, Mango TV, Hồng Quả).
"""
from __future__ import annotations

import re
import urllib.parse

SITES: dict[str, dict] = {
    "Bilibili": {
        "domains": r"(bilibili\.com|b23\.tv|bili2233\.cn)",
        "search": None,  # tìm ngay trong app
    },
    "Douyin (抖音)": {
        "domains": r"(douyin\.com|iesdouyin\.com)",
        "search": "https://www.douyin.com/search/{q}?type=video",
        "note": "Douyin hay đòi cookie: nếu lỗi, chọn trình duyệt ở mục Cookie (nên dùng Firefox) rồi tải lại.",
    },
    "Tây Qua (西瓜视频)": {
        "domains": r"ixigua\.com",
        "search": "https://www.ixigua.com/search/{q}/",
    },
    "Weibo": {
        "domains": r"(weibo\.com|weibo\.cn)",
        "search": "https://s.weibo.com/video?q={q}",
    },
    "Tiểu Hồng Thư (小红书)": {
        "domains": r"(xiaohongshu\.com|xhslink\.com)",
        "search": "https://www.xiaohongshu.com/search_result?keyword={q}",
    },
    "AcFun": {
        "domains": r"acfun\.cn",
        "search": "https://www.acfun.cn/search?keyword={q}",
    },
}

BLOCKED = r"(youtube\.com|youtu\.be|youku\.com|iqiyi\.com|iq\.com|v\.qq\.com|wetv\.vip|mgtv\.com|hongguo)"

URL_RE = re.compile(r"https?://[^\s\"'<>，。！？、）)】]+", re.I)


def search_url(site: str, keyword: str) -> str | None:
    tpl = SITES.get(site, {}).get("search")
    return tpl.format(q=urllib.parse.quote(keyword)) if tpl else None


def site_of(url: str) -> str | None:
    for name, s in SITES.items():
        if re.search(s["domains"], url, re.I):
            return name
    return None


def extract(text: str) -> tuple[str, str]:
    """Lấy link từ đoạn chữ dán vào (kể cả đoạn chia sẻ của app) -> (link, tên trang).
    Mã BV…/av… trần -> Bilibili. Trang không hỗ trợ -> ValueError."""
    t = text.strip()
    for m in URL_RE.finditer(t):
        url = m.group(0).rstrip(".,;")
        if re.search(BLOCKED, url, re.I):
            raise ValueError("Trang này không được hỗ trợ (YouTube và các trang phim VIP/có mã hóa).")
        site = site_of(url)
        if site:
            return url, site
    if re.search(r"BV[0-9A-Za-z]{10}", t) or re.search(r"\bav\d+", t, re.I):
        return t, "Bilibili"
    if URL_RE.search(t):
        raise ValueError("Chưa hỗ trợ trang này. Hỗ trợ: " + ", ".join(SITES))
    raise ValueError("Không thấy link nào. Dán link video hoặc cả đoạn chữ chia sẻ từ app.")


MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def expand(url: str, get=None) -> str:
    """Mở link rút gọn (v.douyin.com, xhslink.com…) thành link đầy đủ mà yt-dlp nhận ra."""
    if not re.search(r"(v\.douyin\.com|xhslink\.com|t\.cn/)", url, re.I):
        return normalize(url)
    if get is None:
        import requests
        def get(u):
            return requests.get(u, headers={"User-Agent": MOBILE_UA}, timeout=15, allow_redirects=True).url
    try:
        final = get(url)
    except Exception:  # noqa: BLE001
        return url
    return normalize(final or url)


def normalize(url: str) -> str:
    """Đưa link Douyin về dạng www.douyin.com/video/<mã> (dạng yt-dlp hiểu chắc chắn)."""
    m = re.search(r"(?:douyin\.com|iesdouyin\.com)/(?:share/)?(?:video|note)/(\d{8,})", url) or \
        re.search(r"douyin\.com/.*?modal_id=(\d{8,})", url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url
