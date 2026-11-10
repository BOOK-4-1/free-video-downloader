"""B 站多 P 清单补充：把「被压扁的单个分 P」还原成完整分 P 列表。

为什么需要这个模块（而不是继续用 yt-dlp）：
- yt-dlp 的 BiliBiliIE 只要看到 URL 带 `?p=N`，就把结果当成单个分 P 返回
  （见 yt-dlp/yt_dlp/extractor/bilibili.py 的 part_id 判定），entries 为空；
- 项目主解析链路又固定 `noplaylist=True`（避免额外请求与 412 拖垮主解析），
  所以拿不到「同一 BV 下还有哪些分 P」。

这里直连 B 站公开的 view 接口，一次请求取回 pages[]（真实分 P 名 + 时长 + cid），
由 downloader.parse() 注入到返回结构的 parts 里，前端「多 P」条与批量下载随即可用。

设计约束（沿用项目既有教训）：
- 纯旁路能力：任何失败都返回空列表，绝不抛异常、绝不拖垮主解析；
- 不引入新依赖（只用 httpx + 标准库），不改 yt-dlp 源码；
- 结果短 TTL 内存缓存，降低重复解析与平台风控压力。
"""
import os
import re
import threading
import time

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
REFERER = "https://www.bilibili.com/"
TIMEOUT = 5          # 旁路请求必须有硬上限，不能吃掉主解析的 45s 预算
ATTEMPTS = 2         # 只重试一次：失败就降级，不做无限重试
CACHE_TTL = int(os.environ.get("BILI_PARTS_TTL", 600))
CACHE_MAX = 64

_VIDEO_RE = re.compile(r"(?:bilibili\.com/video/|b23\.tv/)", re.I)
_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

_cache = {}
_lock = threading.Lock()


def is_video_url(url: str) -> bool:
    """只对 B 站普通视频页生效。

    番剧（bangumi/play）、合集列表（list/medialist）、空间页各有自己的解析通道，
    不应该在这里掺和。
    """
    return bool(_VIDEO_RE.search(url or ""))


def extract_bvid(video_id_or_url: str) -> str:
    """从 yt-dlp 的 id（形如 BV1PwBuBtEZo_p3）或原始链接里取出 bvid。

    yt-dlp 的 BiliBiliIE 无论输入是 av 号还是 b23 短链，最终 info['id'] 都是 bvid，
    所以这里不需要处理短链跳转，也不需要把 av 号换算成 bvid。
    """
    m = _BVID_RE.search(video_id_or_url or "")
    return m.group(1) if m else ""


def _fmt_duration(sec) -> str:
    """与 downloader.fmt_duration 同格式；这里自带一份避免与 downloader 循环导入。"""
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "00:00"
    if sec <= 0:
        return "00:00"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _cookie_header(cookie_file: str) -> str:
    """把项目自己写出的 Netscape cookie 文件转成 HTTP Cookie 头。

    parse() 收到的是临时文件路径（api._ck() 的产物），而直连接口需要头字符串。
    """
    if not cookie_file or not os.path.isfile(cookie_file):
        return ""
    pairs = []
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7 and cols[5]:
                    pairs.append(f"{cols[5]}={cols[6]}")
    except OSError:
        return ""
    return "; ".join(pairs)


def _cache_get(bvid: str):
    with _lock:
        rec = _cache.get(bvid)
        if not rec:
            return None
        ts, parts = rec
        if time.time() - ts > CACHE_TTL:
            _cache.pop(bvid, None)
            return None
        return [dict(p) for p in parts]


def _cache_put(bvid: str, parts: list) -> None:
    with _lock:
        if len(_cache) >= CACHE_MAX:
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest, None)
        _cache[bvid] = (time.time(), parts)


def _request_pages(bvid: str, headers: dict):
    """返回 view 接口的 data 字典；失败返回 None。"""
    for _ in range(ATTEMPTS):
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                r = client.get(API_VIEW, params={"bvid": bvid}, headers=headers)
                if r.status_code != 200:
                    continue
                payload = r.json()
                if payload.get("code") != 0:
                    continue
                return payload.get("data") or {}
        except (httpx.RequestError, ValueError):
            continue
        except Exception:  # noqa: BLE001  任何意外都不许影响主解析
            return None
    return None


def fetch_parts(video_id_or_url: str, cookie_file: str = "") -> list:
    """返回完整分 P 清单；失败或只有一个分 P 时返回 []。

    每项结构与 downloader._shape_single() 的 parts 完全一致：
    {index, id, title, url, duration, duration_text}
    """
    bvid = extract_bvid(video_id_or_url)
    if not bvid:
        return []

    hit = _cache_get(bvid)
    if hit is not None:
        return hit

    headers = {"User-Agent": UA, "Referer": REFERER, "Accept": "application/json"}
    cookie = _cookie_header(cookie_file)
    if cookie:
        headers["Cookie"] = cookie

    data = _request_pages(bvid, headers)
    if not data:
        return []   # 失败不进缓存：下次解析还能再试

    pages = data.get("pages") or []
    out = []
    for i, p in enumerate(pages):
        try:
            page = int(p.get("page") or (i + 1))
        except (TypeError, ValueError):
            page = i + 1
        part = (p.get("part") or "").strip()
        out.append(
            {
                "index": page,
                "id": str(p.get("cid") or ""),
                "title": part or f"P{page}",
                "url": f"https://www.bilibili.com/video/{bvid}?p={page}",
                "duration": int(p.get("duration") or 0),
                "duration_text": _fmt_duration(p.get("duration")),
            }
        )

    if len(out) <= 1:
        return []   # 单 P 视频不显示「多 P」条
    _cache_put(bvid, out)
    return [dict(p) for p in out]
