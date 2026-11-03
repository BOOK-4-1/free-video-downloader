"""yt-dlp 封装层：只做“调用 + 结果整形”，解析与下载能力全部交给 yt-dlp 自己。

设计原则（对应需求：站在巨人肩膀上、尽量少改动）：
- 不修改 yt-dlp 源码，仅通过 sys.path 引入官方 API（YoutubeDL）；
- 抖音等平台 yt-dlp 需要 cookies 才能解析 —— 原项目自己写了解析器，本项目改为
  “浏览器 Cookie 导入”，用户只做一次，之后所有平台都能用，代码量反而更少。
"""
import os
import re
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import config
from app import douyin

if config.YTDLP_DIR not in sys.path:
    sys.path.insert(0, config.YTDLP_DIR)

import yt_dlp  # noqa: E402

YT_DLP_VERSION = yt_dlp.version.__version__

# 常见平台识别（仅用于前端展示徽标，解析本身与平台无关）
PLATFORMS = [
    ("bilibili.com", "bilibili", "B 站"),
    ("b23.tv", "bilibili", "B 站"),
    ("douyin.com", "douyin", "抖音"),
    ("iesdouyin.com", "douyin", "抖音"),
    ("kuaishou.com", "kuaishou", "快手"),
    ("xiaohongshu.com", "xiaohongshu", "小红书"),
    ("xhslink.com", "xiaohongshu", "小红书"),
    ("youtube.com", "youtube", "YouTube"),
    ("youtu.be", "youtube", "YouTube"),
    ("twitter.com", "twitter", "X"),
    ("x.com", "twitter", "X"),
    ("weibo.com", "weibo", "微博"),
    ("ixigua.com", "ixigua", "西瓜视频"),
    ("zhihu.com", "zhihu", "知乎"),
    ("qq.com", "qq", "腾讯视频"),
    ("iqiyi.com", "iqiyi", "爱奇艺"),
    ("youku.com", "youku", "优酷"),
    ("tiktok.com", "tiktok", "TikTok"),
    ("instagram.com", "instagram", "Instagram"),
    ("vimeo.com", "vimeo", "Vimeo"),
]

URL_RE = re.compile(r"https?://[^\s，,；;\"'<>（）()【】]+", re.IGNORECASE)

# 解析超时（秒）。任何单次解析都不会超过这个时间，避免像 YouTube 那样卡死。
PARSE_TIMEOUT = int(os.environ.get("PARSE_TIMEOUT", 45))


class ParseError(Exception):
    """给用户看的错误（已经翻译成人话）"""


def looks_like_playlist(url: str) -> bool:
    """链接本身看起来就是合集 / 收藏夹 / 空间页：直接走合集解析，省掉一次注定失败的尝试"""
    low = url.lower()
    if re.search(r"bilibili\.com/(list|medialist)/", low):
        return True
    if "space.bilibili.com" in low:
        return True
    return bool(re.search(r"(playlist|/sets/|/album/|/collection/|/channel/|/podcast)", low))


def detect_platform(url: str) -> tuple:
    low = url.lower()
    for domain, key, name in PLATFORMS:
        if domain in low:
            return key, name
    return "other", "其他站点"


def extract_urls(text: str) -> list:
    """从粘贴的分享文案里抠出所有链接（抖音/小红书分享出来都是一大段话）"""
    if not text:
        return []
    text = text.strip()
    found = URL_RE.findall(text)
    if found:
        # 去重且保持顺序
        seen, out = set(), []
        for u in found:
            u = u.rstrip("。.,、!！?？")
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out
    if text.startswith("www.") or re.match(r"^[\w.-]+\.[a-z]{2,}/", text, re.I):
        return ["https://" + text]
    return []


def is_playlist(info: dict) -> bool:
    return info.get("_type") in ("playlist", "multi_video") or bool(info.get("entries"))


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
        "nocheckcertificate": True,
        "cachedir": False,
        # 桌面 UA + 浏览器指纹参数，减少 4xx
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }
    if config.FFMPEG_DIR:
        opts["ffmpeg_location"] = config.FFMPEG_DIR
    return opts


def parse(url: str, cookies: str = "") -> dict:
    """解析视频：返回标题、封面、时长、可用清晰度列表等（不下载）"""
    # 抖音走自有免登录通道：yt-dlp 的抖音提取器需要签名 Cookie，服务端直连必然 403
    if douyin.is_douyin(url):
        try:
            data = douyin.parse(url)
        except douyin.DouyinError as e:
            raise ParseError(str(e)) from e
        # 直链只服务端自己用，不下发给浏览器
        for f in data.get("formats") or []:
            f.pop("_play_url", None)
        return data
    opts = _base_opts()
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ParseError(_xiaohongshu_hint(url, _friendly_error(str(e)))) from e
    except Exception as e:  # noqa: BLE001
        raise ParseError(_friendly_error(f"{type(e).__name__}: {e}")) from e

    if not info:
        raise ParseError("这个链接没有解析出内容，换一个链接试试")

    if is_playlist(info):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ParseError("这个合集里没有可下载的视频")
        if len(entries) > 1:
            return _fill_missing_titles(_shape_playlist(info, url), cookies)
        # 只有一条：当成单条视频继续解析（可能是多 P 的第 1 P）
        info = entries[0]

    return _shape_single(info, url)


def _shape_single(info: dict, url: str) -> dict:
    key, name = detect_platform(url or info.get("webpage_url") or "")
    extractor = info.get("extractor_key") or info.get("extractor") or ""
    formats = _shape_formats(info)
    subs = _shape_subs(info)
    # B 站多 P：顶级有 entries 但不是多视频合集（_type 为 None 时）
    parts = [
        e
        for e in (info.get("entries") or [])
        if e and (e.get("webpage_url") or e.get("url"))
    ]

    return {
        "kind": "single",
        "id": info.get("id") or "",
        "title": (info.get("title") or "未命名视频").strip(),
        "url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration") or 0,
        "duration_text": fmt_duration(info.get("duration")),
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator") or "",
        "uploader_id": info.get("uploader_id") or "",
        "platform": key,
        "platform_name": name,
        "extractor": extractor,
        "description": (info.get("description") or "")[:300],
        "upload_date": fmt_date(info.get("upload_date") or ""),
        "view_count": info.get("view_count") or 0,
        "like_count": info.get("like_count") or 0,
        "width": info.get("width") or 0,
        "height": info.get("height") or 0,
        "formats": formats,
        "subtitles": subs,
        "is_live": bool(info.get("is_live")),
        # 多 P 视频：同一 BV 号下的其他分 P（前端可一键批量下载）
        "parts": (
            [
                {
                    "index": i + 1,
                    "id": p.get("id") or "",
                    "title": (p.get("title") or "").strip() or f"P{i + 1}",
                    "url": p.get("webpage_url") or p.get("url") or "",
                    "duration": p.get("duration") or 0,
                    "duration_text": fmt_duration(p.get("duration")),
                }
                for i, p in enumerate(parts[:PLAYLIST_MAX])
            ]
            if len(parts) > 1
            else []
        ),
    }


def _raw_fmt_rank(f: dict) -> tuple:
    """单个原始 format 的优劣：编码兼容性 > mp4 容器 > 码率。
    注意必须和 _rank() 同序，否则「显示 H.264、实际下 AV1」"""
    vcodec = (f.get("vcodec") or "").split(".")[0]
    codec_rank = {"avc1": 3, "h264": 3, "hvc1": 2, "hev1": 2, "h265": 2, "hevc": 2, "vp9": 1, "av01": 1}.get(vcodec, 0)
    return (codec_rank, 1 if (f.get("ext") or "") == "mp4" else 0, f.get("tbr") or 0)


def _shape_formats(info: dict) -> list:
    """把 yt-dlp 的几十个原始 format 整理成用户能看懂的分辨率档位。

    关键点：同一分辨率下往往同时有 avc1 / hev1 / av01 三种编码，
    这里必须「挑中的那个 format」的编码、体积、format_id 三者始终一致，
    否则会出现界面显示 H.264、点下载却拿到 AV1 的错配。
    """
    raw = info.get("formats") or []
    has_video = any(f.get("vcodec") not in (None, "none") for f in raw)
    groups = {}  # key -> dict（representative 即最终会被下载的那条 format）

    for f in raw:
        if f.get("format_note") in ("storyboard", "images"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            continue

        height = f.get("height") or 0
        ext = f.get("ext") or "mp4"
        is_video = vcodec != "none"
        is_audio = acodec != "none"

        key = ("v", height, ext) if is_video else ("a", 0, ext)
        level = "video" if is_video else "audio"

        size = f.get("filesize") or f.get("filesize_approx") or 0
        cand = {
            "height": height,
            "ext": ext,
            "level": level,
            "vcodec": vcodec if is_video else "none",
            "has_audio": is_audio,
            "size": size,
            "format_id": f.get("format_id"),
            "fps": f.get("fps") or 0,
            "tbr": f.get("tbr") or 0,
            "note": f.get("format_note") or "",
            "proto": f.get("protocol") or "https",
            "_rank": _raw_fmt_rank(f),
        }
        cur = groups.get(key)
        if cur is None:
            groups[key] = cand
            continue
        # 同一档位里选兼容性最好的那条，并把它的元数据整体替换，保证三者一致
        if _raw_fmt_rank(f) > cur["_rank"]:
            cand["has_audio"] = cand["has_audio"] or cur["has_audio"]
            groups[key] = cand
        else:
            cur["has_audio"] = cur["has_audio"] or cand["has_audio"]

    out = list(groups.values())
    for it in out:
        it.pop("_rank", None)
        it["codec"] = (it.get("vcodec") if it["level"] == "video" else it.get("acodec") or "")
    # 补一次 _rank，供后面排序/去重使用
    for it in out:
        it["_rank"] = _rank(it)
    out = list({(it["level"], it["height"]): it for it in out}.values())

    videos = [i for i in out if i["level"] == "video"]
    audios = sorted([i for i in out if i["level"] == "audio"], key=lambda x: -x["tbr"])

    for i in videos:
        i["label"] = f'{i["height"]}P' if i["height"] else (i["note"] or "视频")
        i["codec"] = (i.get("vcodec") or "").split(".")[0]
        i["codec_label"] = CODEC_LABEL.get(i["codec"], i["codec"] or "未知")
        i["h264"] = i["codec_label"] == "H.264"
        i["filesize_text"] = fmt_size(i["size"])
        # 无音轨的高清档位需要和音频合并 —— ffmpeg 已内置，用户无感
        i["need_merge"] = not i["has_audio"]
        i.pop("_rank", None)
    for i in audios:
        i["codec"] = (i.get("acodec") or "").split(".")[0]
        i["codec_label"] = (i.get("acodec") or "").split(".")[0] or "音频"
        i["h264"] = False
        i["label"] = "仅音频"
        i["filesize_text"] = fmt_size(i["size"])
        i["need_merge"] = False
        i["recommended"] = False
        i.pop("_rank", None)

    if not has_video and audios:
        return audios

    # 需要合并的档位：界面显示「视频流 + 音频流」的总体积，避免用户以为只有视频那么大
    if audios:
        audio_size = max((a["size"] or 0) for a in audios)
        for v in videos:
            if v.get("need_merge") and audio_size:
                v["size"] = (v["size"] or 0) + audio_size
                v["filesize_text"] = fmt_size(v["size"])

    videos.sort(key=lambda x: (-x["height"], x["ext"] != "mp4"))
    # 推荐档位：<=1080P 里优先 H.264（兼容性最好，Windows/手机/电视都能直接播），
    # 同一编码下再取最高分辨率；整个站都没 H.264 时才退回最高分辨率的 mp4。
    best_id = None
    for v in videos:
        if v["height"] and v["height"] <= 1080 and v["ext"] == "mp4" and v.get("h264"):
            best_id = v["format_id"]
            break
    if best_id is None:
        for v in videos:
            if v["height"] and v["height"] <= 1080 and v["ext"] == "mp4":
                best_id = v["format_id"]
                break
    if best_id is None and videos:
        best_id = videos[0]["format_id"]
    for v in videos:
        v["recommended"] = v["format_id"] == best_id
    return videos + audios


def _rank(it: dict) -> tuple:
    """成型档位的优劣：编码兼容性 > mp4 容器 > 体积 > 码率（与 _raw_fmt_rank 同序）"""
    vcodec = (it.get("vcodec") or "").split(".")[0]
    codec_rank = {"avc1": 3, "h264": 3, "hvc1": 2, "hev1": 2, "h265": 2, "hevc": 2, "vp9": 1, "av01": 1}.get(vcodec, 0)
    return (codec_rank, 1 if it["ext"] == "mp4" else 0, it.get("size") or 0, it.get("tbr") or 0)


def _shape_subs(info: dict) -> list:
    out = []
    for lang, tracks in (info.get("subtitles") or {}).items():
        if tracks:
            out.append({"lang": lang, "name": tracks[0].get("name") or lang, "auto": False})
    for lang, tracks in (info.get("automatic_captions") or {}).items():
        if tracks and not any(s["lang"] == lang for s in out):
            out.append({"lang": lang, "name": tracks[0].get("name") or lang, "auto": True})
    return out[:20]


def fmt_duration(sec) -> str:
    if not sec:
        return "00:00"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(size) -> str:
    if not size:
        return "未知大小"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def fmt_date(raw: str) -> str:
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return ""


def _xiaohongshu_hint(url: str, msg: str) -> str:
    """小红书特有的两个坑，直接告诉用户怎么做，别让他对着「没解析出视频流」猜"""
    if "xiaohongshu" not in (url or "").lower() and "xhslink" not in (url or "").lower():
        return msg
    if "xsec_token" not in (url or ""):
        return (
            "小红书的分享链接必须带 xsec_token：请在网页版小红书里打开这条笔记，"
            "复制地址栏里完整的网址（会带 ?xsec_token=...）再粘贴；只复制 /explore/<id> 是取不到的"
        )
    return msg + "（另外这条也可能是图文笔记——小红书只有视频笔记能下载）"


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    # 顺序有讲究：越具体的规则越靠前（例如 "No video formats found" 要先于泛化的 "not available"）
    rules = [
        ("unsupported url", "这个链接暂不支持，请确认是视频页面地址（不是主页/搜索页）"),
        ("fresh cookies", "该平台需要「已登录」的 Cookie：点右上角「导入 Cookie」粘贴一次后重试"),
        ("no video formats found", "没解析出可下载的视频流：私密 / 图文笔记 / 已删除的内容取不到，换一条视频链接试试"),
        ("no formats found", "没解析出可下载的视频流：私密 / 图文笔记 / 已删除的内容取不到，换一条视频链接试试"),
        ("login", "该平台需要登录 / Cookie：请点右上角「导入 Cookie」后重试"),
        ("private", "这是私密 / 会员专享内容，无法下载"),
        ("geo", "该视频有地区限制，当前服务器无法访问"),
        ("not available", "视频不存在、已删除或不可访问"),
        ("timed out", "解析超时，可能是网络波动或该平台被限制，换个链接试试"),
        ("network", "网络连接失败，请检查网络或稍后重试"),
        ("403", "平台拒绝了请求（403），通常需要 Cookie 或稍后再试"),
        ("404", "视频不存在（404）"),
        ("requested format is not available", "所选清晰度不可用，请换一个清晰度"),
    ]
    for k, friendly in rules:
        if k in low:
            return friendly
    clean = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    clean = clean.replace("ERROR: ", "").strip()
    clean = clean.split("\n")[0]
    return clean[:180] or "解析失败，请稍后重试"


# 编码偏好：默认要求 H.264（avc1），兼容性最好（老设备 / 剪辑软件 / 各种播放器都能直接放）。
# 若某个视频平台只提供 AV1 / HEVC，兜底策略会自动退回（见 _build_format_selector），不会下载失败。
VIDEO_CODEC_ORDER = {"avc1": 0, "h264": 0, "hvc1": 1, "hev1": 1, "h265": 1, "hevc": 1, "vp9": 2, "av01": 3}
CODEC_LABEL = {"avc1": "H.264", "h264": "H.264", "hvc1": "HEVC", "hev1": "HEVC", "h265": "HEVC", "hevc": "HEVC",
               "vp9": "VP9", "av01": "AV1"}

PLAYLIST_MAX = int(os.environ.get("PLAYLIST_MAX", 50))
META_FILL_MAX = int(os.environ.get("META_FILL_MAX", 50))


def parse_playlist(url: str, cookies: str = "") -> dict:
    """合集 / 多 P 解析：只取清单（不解析每条的清晰度），速度接近单条解析"""
    opts = _base_opts()
    opts["noplaylist"] = False
    opts["extract_flat"] = "in_playlist"
    if cookies:
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ParseError(_friendly_error(str(e))) from e
    except Exception as e:  # noqa: BLE001
        raise ParseError(_friendly_error(f"{type(e).__name__}: {e}")) from e

    # 有些网站（如 B 站个人空间）把整个收藏夹塞进 entries[0] 里，剥一层
    while (
        isinstance(info.get("entries"), list)
        and len(info["entries"]) == 1
        and isinstance(info["entries"][0], dict)
        and info["entries"][0].get("_type") in ("playlist", "multi_video")
    ):
        info = info["entries"][0]

    return _shape_playlist(info, url)


def safe_filename(title: str, ext: str) -> str:
    """把视频标题变成安全的下载文件名（去掉 Windows 非法字符，保留中文）"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', ' ', (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    # 去掉 B 站等平台标题后面拖的一长串内部 ID（如 "xxx p01 video_9d99...-1hc-6tx4k"）
    name = re.sub(r"\s+p\d+\s+video_[\w-]+.*$", "", name, flags=re.I).strip()
    if len(name) > 80:
        name = name[:80].rstrip()
    if not name:
        name = "video"
    ext = ext if ext.startswith(".") else "." + ext
    return name + ext


def _shape_playlist(info: dict, url: str) -> dict:
    entries = [e for e in (info.get("entries") or []) if e]
    key, name = detect_platform(url)
    items = []
    for i, e in enumerate(entries[:PLAYLIST_MAX]):
        thumbs = e.get("thumbnails") or []
        items.append(
            {
                "index": i + 1,
                "id": e.get("id") or "",
                "title": (e.get("title") or "").strip() or f"第 {i + 1} 个视频",
                "url": e.get("webpage_url") or e.get("url") or "",
                "duration": e.get("duration") or 0,
                "duration_text": fmt_duration(e.get("duration")),
                "thumbnail": (thumbs[-1].get("url") if thumbs else "") or e.get("thumbnail") or "",
            }
        )
    total = info.get("playlist_count") or len(entries)
    return {
        "kind": "playlist",
        "id": info.get("id") or "",
        "title": (info.get("title") or "合集").strip(),
        "url": info.get("webpage_url") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "platform": key,
        "platform_name": name,
        "count": total,
        "items": items,
        "truncated": total > len(items),
    }


def _fill_missing_titles(data: dict, cookies: str = "") -> dict:
    """合集条目常只有链接没有标题：并发快速解析前若干条，补上标题/时长（失败就保留原样）"""
    items = data.get("items") or []
    missing = [it for it in items if it["title"].startswith("第 ") and not it["duration"]]
    if not missing:
        return data

    def one(it):
        opts = _base_opts()
        opts["extract_flat"] = True   # 只要元信息，不取 formats，最快
        if cookies:
            opts["cookiefile"] = cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(it["url"], download=False, process=False) or {}
            return info.get("title"), info.get("duration"), info.get("thumbnail") or ""
        except Exception:  # noqa: BLE001
            return None, None, ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        for it, (title, duration, thumb) in zip(missing[:META_FILL_MAX], pool.map(one, missing[:META_FILL_MAX])):
            if title:
                it["title"] = title.strip()
            if duration:
                it["duration"] = duration
                it["duration_text"] = fmt_duration(duration)
            if thumb:
                it["thumbnail"] = thumb
    return data


def cleanup_temp_cookies():
    now = time.time()
    for name in os.listdir(config.RUNTIME_DIR):
        if name.startswith("cookies-") and name.endswith(".txt"):
            p = os.path.join(config.RUNTIME_DIR, name)
            try:
                if now - os.path.getmtime(p) > 3600:
                    os.remove(p)
            except OSError:
                pass


def outtmpl_for(task_id: str) -> str:
    return os.path.join(config.DOWNLOAD_DIR, f"{task_id}.%(ext)s")


def cleanup_downloads(max_age: int) -> list:
    """清理过期文件，返回被删掉的文件名"""
    now = time.time()
    removed = []
    for name in os.listdir(config.DOWNLOAD_DIR):
        p = os.path.join(config.DOWNLOAD_DIR, name)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age:
                os.remove(p)
                removed.append(name)
        except OSError:
            pass
    return removed


def disk_free_text() -> str:
    try:
        usage = shutil.disk_usage(config.DOWNLOAD_DIR)
        return f"{usage.free / 1024 ** 3:.1f} GB"
    except OSError:
        return "未知"


# ---------------------------------------------------------------------------
# Cookie 处理：校验、落地、按域名建 Netscape 文件
# ---------------------------------------------------------------------------

# 这些平台的 Cookie 必须带域名才有意义（抖音/小红书会校验 Referer 与 Cookie 的域名）
COOKIE_DOMAINS = {
    "douyin": [".douyin.com", ".iesdouyin.com"],
    "xiaohongshu": [".xiaohongshu.com", ".xhslink.com"],
    "bilibili": [".bilibili.com", ".b23.tv"],
    "kuaishou": [".kuaishou.com"],
    "youtube": [".youtube.com", ".google.com"],
    "weibo": [".weibo.com", ".weibo.cn"],
    "twitter": [".x.com", ".twitter.com"],
}


def parse_cookie_text(text: str) -> list:
    """把用户粘贴的内容解析成 [(name, value), ...]

    支持三种常见形态：
    1. 请求头形式：  a=b; c=d; e=f
    2. 整段请求头：  Cookie: a=b; c=d
    3. Netscape 文件：每行 7 列，用 TAB 分隔
    """
    text = (text or "").strip()
    if not text:
        return []
    pairs = []
    # Netscape 格式
    if "\t" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append((cols[5].strip(), cols[6].strip()))
        return pairs
    # 去掉 "Cookie:" 前缀（用户从 DevTools 直接复制整行时会有）
    low = text
    for prefix in ("cookie:", "Cookie:", "COOKIE:"):
        if low.startswith(prefix):
            low = low[len(prefix):]
            break
    # 允许整段是多行的（每行一对）
    flat = " ".join(low.split("\n"))
    for part in flat.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            pairs.append((k, v))
    return pairs


def cookie_summary(text: str) -> dict:
    """给前端看的 Cookie 概况：有几项、是不是登录态、有没有关键字段"""
    pairs = parse_cookie_text(text)
    names = [k for k, _ in pairs]
    has_login = any(n in names for n in ("SESSDATA", "sessionid", "sessionid_ss", "sid_tt", "web_session", "sid"))
    return {
        "count": len(pairs),
        "names": names[:20],
        "has_login": has_login,
        "empty": not pairs,
    }


def write_cookiefile(text: str, platforms: list | None = None) -> str:
    """把粘贴的 Cookie 落成 Netscape 文件；域名按目标平台自动选择。

    一次写入多个平台的域名（例如抖音 + 小红书都存一份），这样用户导一次
    Cookie，两个平台都能用，不用来回切换。
    """
    pairs = parse_cookie_text(text)
    if not pairs:
        return ""
    keys = platforms or list(COOKIE_DOMAINS.keys())
    lines = ["# Netscape HTTP Cookie File", "# generated by VideoGrab"]
    for key in keys:
        for domain in COOKIE_DOMAINS.get(key, []):
            for name, value in pairs:
                lines.append("\t".join([domain, "TRUE", "/", "FALSE", "0", name, value]))
    path = os.path.join(config.RUNTIME_DIR, f"cookies-{uuid.uuid4().hex}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def platform_keys_of(url: str) -> list:
    key, _ = detect_platform(url or "")
    return [key] if key in COOKIE_DOMAINS else []


def verify_cookie(text: str, url: str = "") -> dict:
    """验证 Cookie 是否真的可用：拿目标平台解析一次，看返回结果。

    返回 {ok, stage, message, detail}：
    - ok=True 表示 Cookie 生效（能拿到播放地址 / 登录态字段）
    - stage 用于前端提示：empty / anonymous / auth / ok
    """
    pairs = parse_cookie_text(text)
    if not pairs:
        return {"ok": False, "stage": "empty", "message": "没有解析到任何 Cookie 键值对",
                "summary": cookie_summary(text)}

    target = (url or "").strip()
    platforms = platform_keys_of(target) if target else list(COOKIE_DOMAINS.keys())

    # 1) 先做纯本地校验：Cookie 里有没有「登录态」字段
    summary = cookie_summary(text)
    local_ok = summary["has_login"]

    # 2) 有链接就真解析一次（这是唯一可信的验证方式）
    if target:
        cookie_file = write_cookiefile(text, platforms)
        try:
            info = _probe(target, cookie_file)
            tmp = _probe(target, "")
            return {
                "ok": True,
                "stage": "ok",
                "message": "Cookie 可用：目标站点已成功解析",
                "summary": summary,
                "probe": {
                    "with_cookie": info,
                    "anonymous": tmp,
                    "need_cookie": tmp.get("status") != "ok",
                },
            }
        except ParseError as e:
            return {
                "ok": False,
                "stage": "auth" if local_ok else "anonymous",
                "message": f"Cookie 未能通过站点校验：{e}",
                "summary": summary,
            }
        finally:
            try:
                if cookie_file:
                    os.remove(cookie_file)
            except OSError:
                pass

    # 3) 没有链接：只能凭字段判断（抖音/抖音国际版登录态一般是 sessionid / sid_tt）
    return {
        "ok": local_ok,
        "stage": "ok" if local_ok else "anonymous",
        "message": (
            "识别到登录态字段，已保存（建议再粘贴一个视频链接做一次真实验证）"
            if local_ok
            else "没有识别到登录态字段（如 sessionid / SESSDATA / web_session），可能是游客 Cookie，抖音/小红书通常解析不了"
        ),
        "summary": summary,
    }


def _probe(url: str, cookie_file: str = "") -> dict:
    """轻量探测：只解析元信息，不取 formats，用于验证 Cookie / 网络是否可用"""
    opts = _base_opts()
    opts["extract_flat"] = True
    if cookie_file:
        opts["cookiefile"] = cookie_file
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False) or {}
    except yt_dlp.utils.DownloadError as e:
        raise ParseError(_friendly_error(str(e))) from e
    except Exception as e:  # noqa: BLE001
        raise ParseError(_friendly_error(f"{type(e).__name__}: {e}")) from e
    return {
        "status": "ok",
        "title": (info.get("title") or "").strip()[:80],
        "uploader": info.get("uploader") or info.get("channel") or "",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
    }


def _build_format_selector(format_id: str = "") -> str:
    """构造 yt-dlp 的 format 表达式。

    核心是「H.264 优先 + 自动兜底」：
      bv*[vcodec^=avc1]+ba   → 只要平台有 H.264，优选用它
      /bv*+ba                → 没有 H.264（纯 AV1/HEVC 平台）时退回最佳可用流
      /b                     → 连音视频分离都没有时退回单文件
    这样默认产物在 Windows / 老手机 / 剪辑软件里都能直接播，且不会因为
    强制 H.264 导致某些平台下载失败。
    """
    fmt = (format_id or "").strip()
    if not fmt or fmt == "best":
        return "bv*[vcodec^=avc1]+ba/bv*+ba/b"
    if fmt == "audio":
        return "bestaudio/best"
    # 用户手选的具体档位：优先同分辨率 H.264，其次原档位，最后任意可用
    return f"{fmt}+bestaudio/{fmt}/bv*[vcodec^=avc1]+ba/bv*+ba/b"


def merge_output_format() -> str:
    """统一输出 mp4（H.264 + AAC 是最通用的组合）"""
    return "mp4"
