"""抖音免登录解析通道（自写，不依赖 yt-dlp 的抖音提取器）

为什么单独写：
    yt-dlp 的 DouyinIE 只调 `aweme/v1/web/aweme/detail/` 接口，该接口需要
    UIFID / a_bogus / __ac_signature 等签名，服务端直连恒返回
    403 Blocked by ArgusSecurityPlugin，即使带上完整浏览器 Cookie 也一样。
    与其去逆向签名，不如走抖音自己的移动端分享页：
    页面 HTML 里内联了 `window._ROUTER_DATA`，里面就是完整的视频信息，
    无需登录、无需 Cookie。

画质：
    播放地址是 `aweme/v1/play/?...&ratio=720p&...`，
    把 ratio 改成 1080p 就能拿到 1080P 无水印源（实测 1920x1080 h264+aac）；
    平台没有更高清晰度时服务端会自动回落到源画质，不会报错。
    把 `/playwm/` 换成 `/play/` 就是去水印版本。

稳定性：
    全新 Session 第一次请求常常拿不到 videoInfoRes（只下发 ttwid cookie），
    同一 Session 第二次起稳定成功 —— 所以这里固定「同一个 Session 重试若干次」。
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 可选清晰度：(真实高度, 播放地址里的 ratio 值)
# 注意 ratio=540p 实际下发 1024x576，所以高度按真实的 576 标，避免界面和文件对不上
GEARS = [(576, "540p"), (720, "720p"), (1080, "1080p")]

SHARE_TMPL = "https://www.iesdouyin.com/discover?modal_id={vid}"
ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\});?\s*</script>", re.S)
ID_RE = re.compile(r"(?:/video/|/share/video/|modal_id=|/note/)(\d{6,})")

CHUNK = 256 * 1024


class DouyinError(Exception):
    """给用户看的错误（已经翻译成人话）"""


def is_douyin(url: str) -> bool:
    low = (url or "").lower()
    return any(d in low for d in ("douyin.com", "iesdouyin.com"))


def _http() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return s


def extract_id(url: str) -> str:
    """从各种形态的抖音链接里取 aweme_id

    支持 /video/<id>、share/video/<id>、?modal_id=<id>、/note/<id>
    """
    m = ID_RE.search(url or "")
    return m.group(1) if m else ""


def resolve_id(url: str, session: requests.Session) -> str:
    """短链（v.douyin.com/xxx）要先跟一次跳转才知道 id"""
    vid = extract_id(url)
    if vid:
        return vid
    low = (url or "").lower()
    if "v.douyin.com" in low or "v.iesdouyin.com" in low:
        try:
            r = session.get(url, timeout=15, allow_redirects=True)
        except requests.RequestException as e:
            raise DouyinError(f"抖音短链打不开：{type(e).__name__}") from e
        vid = extract_id(r.url) or extract_id(r.text)
        if not vid:
            raise DouyinError("抖音短链没跳到视频页，可能已失效；请在抖音里复制「分享链接」再试")
        return vid
    raise DouyinError("没认出抖音视频 ID：请粘贴形如 https://www.douyin.com/video/7123... 的链接")


def _fetch_item(session: requests.Session, vid: str, tries: int = 4) -> dict:
    """拿 _ROUTER_DATA 里的 item_list[0]（同一个 Session 重试，首轮常常拿不到）"""
    last_reason = ""
    for i in range(tries):
        try:
            r = session.get(SHARE_TMPL.format(vid=vid), timeout=20)
        except requests.RequestException as e:
            last_reason = f"{type(e).__name__}"
            time.sleep(0.6)
            continue
        m = ROUTER_RE.search(r.text)
        if not m:
            last_reason = "页面结构变了"
            time.sleep(0.6)
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            last_reason = "数据解析失败"
            time.sleep(0.6)
            continue
        for val in (data.get("loaderData") or {}).values():
            if not isinstance(val, dict):
                continue
            vir = val.get("videoInfoRes") or {}
            items = vir.get("item_list") or []
            if items:
                return items[0]
            for f in vir.get("filter_list") or []:
                reason = (f.get("filter_reason") or "") + " " + (f.get("detail_msg") or "")
                if "NOT_EXIST" in reason or "DELETE" in reason:
                    raise DouyinError("这条抖音视频不存在或已被作者删除")
                if "PRIVATE" in reason or "FRIEND" in reason:
                    raise DouyinError("这是私密 / 仅好友可见的抖音视频，下载不了")
                last_reason = reason.strip()[:60]
        time.sleep(0.5)
    raise DouyinError(
        "抖音这次没返回视频信息（平台风控或链接失效）"
        + (f"：{last_reason}" if last_reason else "")
        + "；把链接重新复制一次再试通常就好了"
    )


def _clean_play_url(raw: str) -> str:
    """去水印：播放页地址里的 playwm 换成 play"""
    return (raw or "").replace("/playwm/", "/play/").replace("/playwm?", "/play?")


def _url_for_ratio(base: str, ratio: str) -> str:
    if "ratio=" in base:
        return re.sub(r"ratio=[^&]*", "ratio=" + ratio, base)
    return base + (("&" if "?" in base else "?") + "ratio=" + ratio)


def _probe_size(session: requests.Session, url: str) -> int:
    """HEAD + Range 探测真实体积（结果可能被重定向到 CDN，所以跟到最终地址）"""
    try:
        h = session.head(url, timeout=12, allow_redirects=True, headers={"Range": "bytes=0-1"})
        cr = h.headers.get("content-range") or ""
        if "/" in cr:
            total = cr.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        n = h.headers.get("content-length") or ""
        if n.isdigit() and h.status_code in (200, 206):
            return int(n)
    except requests.RequestException:
        pass
    return 0


def fmt_duration(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_size(size: int) -> str:
    if not size:
        return "未知大小"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def parse(url: str) -> dict:
    """解析抖音链接，返回与 yt-dlp 通道完全一致的结构（前端无差别渲染）"""
    session = _http()
    vid = resolve_id(url, session)
    item = _fetch_item(session, vid)

    video = item.get("video") or {}
    base = _clean_play_url(((video.get("play_addr") or {}).get("url_list") or [""])[0])
    if not base:
        raise DouyinError("这条抖音内容没有可下载的视频流（可能是图集）")

    src_h = int(video.get("height") or 1080)
    duration = int(video.get("duration") or 0) / 1000.0

    covers = ((video.get("cover") or {}).get("url_list") or [])
    thumb = covers[0] if covers else ""
    if not thumb:
        thumbs = ((video.get("big_thumbs") or []) or [])
        thumb = ((thumbs[0] or {}).get("img_urls") or [""])[0] if thumbs else ""

    # 只提供源画质及以下的档位；源是 720P 时别给用户一个下下来还是 720P 的「1080P」
    gears = [(h, name) for (h, name) in GEARS if h <= max(src_h, 576)]
    if not gears:
        gears = [(576, "540p")]

    formats = []
    jobs = sorted(gears, reverse=True)          # 高度从高到低
    # 体积探测是 3 个独立 HEAD 请求，并发做，否则解析要多等好几秒
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        sizes = list(pool.map(lambda g: _probe_size(session, _url_for_ratio(base, g[1])), jobs))

    # 抖音的转码档位偶尔不合常理（实测 ratio=540p 下发 1024x576 且体积比 720p 还大），
    # 这种「分辨率更低、体积更大」的档位对用户没有意义，直接剔除，避免选了更糊的文件还更大
    best_size = -1
    kept = []
    for (h, name), size in zip(jobs, sizes):
        if size and best_size > 0 and size >= best_size:
            continue
        best_size = size or best_size
        kept.append(((h, name), size))

    for (h, name), size in kept:
        u = _url_for_ratio(base, name)
        formats.append(
            {
                "height": h,
                "ext": "mp4",
                "level": "video",
                "vcodec": "h264",       # 抖音直链实测都是 h264 + aac
                "acodec": "aac",
                "has_audio": True,
                "size": size,
                "format_id": f"dy-{name}",
                "fps": 30,
                "tbr": round(size * 8 / duration / 1000, 1) if size and duration else 0,
                "note": "",
                "proto": "https",
                "codec": "h264",
                "codec_label": "H.264",
                "h264": True,
                "label": f"{h}P",
                "filesize_text": _fmt_size(size),
                "need_merge": False,
                "recommended": False,
                "_play_url": u,
            }
        )
    # 推荐：<=1080P 里最高的那档（清晰度和体积平衡）
    pick = next((f for f in formats if f["height"] <= 1080), formats[0] if formats else None)
    if pick:
        pick["recommended"] = True

    return {
        "kind": "single",
        "id": vid,
        "title": (item.get("desc") or "").strip() or "抖音视频",
        "url": f"https://www.douyin.com/video/{vid}",
        "thumbnail": thumb,
        "duration": duration,
        "duration_text": fmt_duration(duration),
        "uploader": ((item.get("author") or {}).get("nickname") or "").strip(),
        "uploader_id": str((item.get("author") or {}).get("uid") or ""),
        "platform": "douyin",
        "platform_name": "抖音",
        "extractor": "DouyinMobile",
        "description": (item.get("desc") or "")[:300],
        "upload_date": "",
        "view_count": 0,
        "like_count": int((item.get("statistics") or {}).get("digg_count") or 0),
        "width": int(video.get("width") or 0),
        "height": src_h,
        "formats": formats,
        "subtitles": [],
        "is_live": False,
        "parts": [],
        # 复用 yt-dlp 通道的默认选择逻辑：没手选档位时用推荐档
        "_default_format": (pick or {}).get("format_id", ""),
        "via": "douyin-mobile",   # 标记走了自有通道
    }


def play_url_of(format_id: str, detail: dict = None) -> str:
    """根据档位 id 反查真实播放地址（detail 里带 _play_url 时直接用）"""
    if detail:
        for f in detail.get("formats") or []:
            if f.get("format_id") == format_id and f.get("_play_url"):
                return f["_play_url"]
    return ""


def download(url: str, format_id: str, dest_path: str, on_progress=None, cancel=None) -> str:
    """直链下载（带进度回调）。不走 yt-dlp，因为这里本来就是单个 mp4 直链。

    on_progress(done_bytes, total_bytes, speed_str, eta_str)
    cancel() -> bool，返回 True 时中断
    """
    session = _http()
    vid = resolve_id(url, session)
    ratio = "1080p"
    m = re.match(r"dy-(\d+p)", format_id or "")
    if m:
        ratio = m.group(1)

    item = _fetch_item(session, vid)
    video = item.get("video") or {}
    base = _clean_play_url(((video.get("play_addr") or {}).get("url_list") or [""])[0])
    if not base:
        raise DouyinError("这条抖音内容没有可下载的视频流")
    target = _url_for_ratio(base, ratio)

    started = time.time()
    try:
        with session.get(target, timeout=30, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest_path, "wb") as fh:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if cancel and cancel():
                        raise DouyinError("任务已被清理")
                    if on_progress:
                        elapsed = max(time.time() - started, 0.001)
                        speed = done / elapsed
                        eta = (total - done) / speed if total and speed else 0
                        on_progress(done, total, f"{speed / 1024 / 1024:.1f} MB/s", f"{int(eta)}s")
    except requests.RequestException as e:
        raise DouyinError(f"抖音视频下载失败：{type(e).__name__}") from e

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) < 1024:
        raise DouyinError("抖音视频下载下来是空文件，换一档清晰度或稍后重试")
    return dest_path
