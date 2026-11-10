"""字幕提取层：把 yt-dlp 返回的字幕轨道归一化成带时间戳的分段。

一期边界：只处理平台字幕，不做语音转写（ASR）。

为什么单独一层：
    downloader._shape_single() 只保留字幕清单、把正文丢了。这里把正文捡回来
    并缓存 —— 不落盘字幕文件，SRT / VTT / B 站 JSON 自带时间码，正好就是
    「时间戳章节」的数据源。

    注意 yt-dlp 只有在 writesubtitles / listsubtitles 开启时才会调用提取器的
    _get_subtitles()，因此只有 extract_raw() 开启该选项（B 站除外，走直连兜底）；
    parse() 主解析链路**绝不**开启 —— B 站提取器开字幕会额外请求 x/player/wbi/v2，
    带登录态时该接口常返回 412，会把整次解析拖垮。

    轨道形态：B 站 / 部分平台给 data（正文已在内存），YouTube / TED 给 url
    （按需拉一次，TED 还可能是 HLS m3u8 分片）。B 站的 wbi 接口常被平台 412
    拦截，故额外提供 fetch_bilibili_direct() 直连兜底。
"""
import hashlib
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx

from app import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SKIP_LANGS = {"danmaku"}  # B 站弹幕不是字幕
MAX_HLS_SEGMENTS = 80     # HLS 字幕播放列表最多拉多少个分片，防恶意/异常列表
EXT_ORDER = {"srt": 0, "vtt": 1, "json3": 2, "srv1": 3, "srv2": 4, "srv3": 5, "ttml": 6, "xml": 7}
TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})"
)
TAG_RE = re.compile(r"<[^>]+>")

# 字幕正文缓存：key -> {ts, info}。只存字幕相关的少量字段，不存整个 info（含几十条 formats）
_cache = {}
# 已解析结果缓存：HLS 类字幕要拉几十个分片，解析一次就复用，避免 /ai/subtitle 与 /ai/summary 各拉一遍
_resolved_cache = {}
_RESOLVED_MAX = 20
_lock = threading.Lock()


def _clock(s) -> float:
    """把 00:01:02,500 / 00:01:02.500 / 01:02.500 转成秒"""
    s = (s or "").strip()
    frac = re.search(r"[,.](\d{1,3})", s)
    bits = re.split(r"[,.]", s)[0].split(":")
    try:
        if len(bits) == 3:
            h, m, sec = (int(x) for x in bits)
        elif len(bits) == 2:
            h, m, sec = 0, int(bits[0]), int(bits[1])
        else:
            return 0.0
    except ValueError:
        return 0.0
    return float(h * 3600 + m * 60 + sec) + (float("0." + frac.group(1)) if frac else 0.0)


def _clean(s) -> str:
    s = html.unescape(s or "")
    s = TAG_RE.sub("", s)
    s = s.replace("\\N", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _mmss(sec) -> str:
    s = int(sec or 0)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def parse_srt(text) -> list:
    out = []
    normalized = (text or "").replace("\r\n", "\n")
    for block in re.split(r"\n\s*\n", normalized):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        idx = 1 if lines[0].strip().isdigit() else 0
        m = TIME_RANGE_RE.search(lines[idx])
        if not m:
            continue
        body = _clean("<br>".join(ln.strip() for ln in lines[idx + 1:]))
        if body:
            out.append({"start": _clock(m.group(1)), "end": _clock(m.group(2)), "text": body})
    return out


def parse_vtt(text) -> list:
    """VTT 与 SRT 结构基本一致，先剔除 WEBVTT / NOTE / STYLE / REGION 元数据块"""
    kept, skip = [], False
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        stripped = ln.strip()
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            skip = True
            continue
        if skip:
            if not stripped:
                skip = False
            continue
        if stripped.startswith("WEBVTT"):
            continue
        kept.append(ln)
    return parse_srt("\n".join(kept))


def parse_json3(text) -> list:
    """兼容 B 站 body 格式与 YouTube json3 events 格式"""
    out = []
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return out
    for line in data.get("body") or []:
        try:
            body = _clean(line.get("content") or "")
            if body:
                out.append({"start": float(line.get("from") or 0), "end": float(line.get("to") or 0), "text": body})
        except (TypeError, ValueError, AttributeError):
            continue
    if out:
        return out
    for ev in data.get("events") or []:
        body = _clean("".join((s.get("utf8") or "") for s in (ev.get("segs") or [])))
        if not body or body == "\n":
            continue
        start = (ev.get("tStartMs") or 0) / 1000.0
        out.append({"start": start, "end": start + (ev.get("dDurationMs") or 0) / 1000.0, "text": body})
    return out


def parse_xml(text) -> list:
    """YouTube srv1 / TTML：<text start dur> 或 <p begin end>"""
    import xml.etree.ElementTree as ET

    out = []
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError:
        return out
    for el in root.iter():
        if el.tag.split("}")[-1].lower() not in ("text", "p"):
            continue
        raw_start = el.get("start") or el.get("begin") or "0"
        raw_end = el.get("end") or ""
        try:
            start = float(raw_start)
        except ValueError:
            start = _clock(raw_start)
        if raw_end:
            try:
                end = float(raw_end)
            except ValueError:
                end = _clock(raw_end)
        else:
            try:
                end = start + float(el.get("dur") or 0)
            except ValueError:
                end = start
        body = _clean("".join(el.itertext()))
        if body:
            out.append({"start": start, "end": end, "text": body})
    return out


def parse_subtitle(text, ext="") -> list:
    e = (ext or "").lower()
    head = (text or "").lstrip()[:16]
    if e == "json3" or head.startswith(('{"body"', '{"events"')):
        segs = parse_json3(text)
        if segs:
            return segs
    if e in ("xml", "srv1", "srv2", "srv3", "ttml") or head.startswith("<?xml"):
        segs = parse_xml(text)
        if segs:
            return segs
    if e == "vtt" or head.startswith("WEBVTT"):
        return parse_vtt(text)
    return parse_srt(text)


def _lang_rank(lang, auto) -> tuple:
    """人工字幕优先于自动字幕；中文优先于英文；zh-CN 优先于 B 站 AI 字幕 ai-zh"""
    l = (lang or "").lower()
    if l in ("zh-cn", "zh-hans", "zh"):
        base = 0
    elif l in ("ai-zh", "zh-hant", "zh-tw", "zh-hk"):
        base = 1
    elif l.startswith("zh"):
        base = 2
    elif l.startswith("en"):
        base = 3
    else:
        base = 4
    return (1 if auto else 0, base)


def track_list(info) -> list:
    """把 yt-dlp 的 subtitles / automatic_captions 拍平成可用轨道清单"""
    out = []
    for auto, field in ((False, "subtitles"), (True, "automatic_captions")):
        for lang, tracks in ((info or {}).get(field) or {}).items():
            if not tracks or (lang or "").lower() in SKIP_LANGS:
                continue
            cands = [t for t in tracks if isinstance(t, dict)]
            if not cands:
                continue
            # 同一语言多种格式：优先带 data 的（免请求），其次按扩展名兼容性
            cands.sort(key=lambda t: (0 if t.get("data") else 1, EXT_ORDER.get((t.get("ext") or "").lower(), 9)))
            t = cands[0]
            out.append({
                "lang": lang,
                "name": t.get("name") or lang,
                "auto": auto,
                "ext": (t.get("ext") or "").lower(),
                "data": t.get("data") or "",
                "url": t.get("url") or "",
            })
    return out


def pick_track(tracks):
    usable = [t for t in tracks if t.get("data") or t.get("url")]
    if not usable:
        return None
    usable.sort(key=lambda t: _lang_rank(t["lang"], t["auto"]))
    return usable[0]


def no_subtitle_hint(url, has_cookie=False) -> str:
    """无字幕时给人话，不让人对着「解析失败」猜原因（沿用项目错误翻译表的风格）"""
    low = (url or "").lower()
    tail = "本期 AI 总结只基于字幕工作，暂不做语音转写。"
    if "douyin" in low or "iesdouyin" in low:
        return "抖音视频没有可用的字幕轨，" + tail + "抖音内容暂时无法总结。"
    if "xiaohongshu" in low or "xhslink" in low:
        return "小红书笔记一般不提供字幕，" + tail + "该平台暂时无法总结。"
    if "bilibili" in low or "b23.tv" in low:
        if not has_cookie:
            return (
                "这条 B 站视频没取到字幕。B 站字幕通常需要登录态：点右上角「导入 Cookie」粘贴含 "
                "SESSDATA 的 Cookie 后重新解析再试；也可能是 UP 主未上传、平台也没生成 AI 字幕。" + tail
            )
        return "这条 B 站视频没有可用字幕（UP 主未上传、平台也没生成 AI 字幕），" + tail
    return "这条视频没有可用的字幕轨，" + tail


def _key(url, has_cookie, salt="") -> str:
    """缓存键带 Cookie 指纹：换了登录信息就不能复用上一次的字幕结果。

    B 站 AI 字幕会串轨，同一 URL 在不同登录态下可能拿到完全不同的内容，
    只按 url + has_cookie 布尔值缓存会把错误结果锁死 30 分钟。
    """
    return hashlib.md5(f"{url}|{int(bool(has_cookie))}|{salt}".encode("utf-8")).hexdigest()


def cache_put(url, has_cookie, info) -> None:
    """解析过一次就把字幕正文存内存，AI 总结直接复用，避免重复 extract_info"""
    if not info:
        return
    with _lock:
        if len(_cache) >= config.SUBTITLE_CACHE_MAX:
            oldest = sorted(_cache, key=lambda k: _cache[k]["ts"])[: len(_cache) - config.SUBTITLE_CACHE_MAX + 1]
            for k in oldest:
                _cache.pop(k, None)
        _cache[_key(url, has_cookie)] = {
            "ts": time.time(),
            "info": {
                "title": info.get("title") or "",
                "duration": info.get("duration") or 0,
                "subtitles": info.get("subtitles") or {},
                "automatic_captions": info.get("automatic_captions") or {},
            },
        }


def cache_get(url, has_cookie):
    with _lock:
        k = _key(url, has_cookie)
        item = _cache.get(k)
        if not item:
            return None
        if time.time() - item["ts"] > config.SUBTITLE_TTL:
            _cache.pop(k, None)
            return None
        return item["info"]


def cache_stats() -> dict:
    with _lock:
        return {"size": len(_cache), "ttl": config.SUBTITLE_TTL, "max": config.SUBTITLE_CACHE_MAX}


def parse_m3u8(text, base_url) -> list:
    """HLS 字幕播放列表 → 分片绝对地址（跳过 # 注释与空行）"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(urljoin(base_url or "", line))
    return out[:MAX_HLS_SEGMENTS]


def _fetch_hls(playlist, playlist_url, cookies_text="", referer="") -> str:
    """并发拉回所有 vtt 分片并合并；单个分片失败不影响整体。

    TED 等站点给的是 m3u8 清单而不是字幕文件本身，分片里的时间码是绝对时间，
    直接按顺序拼接即可，不需要额外做时间偏移。
    """
    urls = parse_m3u8(playlist, playlist_url)
    if not urls:
        return ""

    def one(u):
        try:
            return _fetch_url(u, cookies_text, referer)
        except Exception:  # noqa: BLE001
            return ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        parts = list(pool.map(one, urls))
    return "\n\n".join(p for p in parts if p and p.strip())


def _referer_for(url) -> str:
    low = (url or "").lower()
    if "bilibili" in low or "b23.tv" in low:
        return "https://www.bilibili.com/"
    if "youtube" in low or "youtu.be" in low:
        return "https://www.youtube.com/"
    return ""


def _fetch_url(url, cookies_text="", referer="") -> str:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if cookies_text:
        headers["Cookie"] = cookies_text
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.text


def _load_info(url, cookie_file=""):
    """缓存没命中时重新 extract_info 一次，返回 (info, error)。

    延迟导入避免与 downloader 循环引用。extract_raw 解析失败时返回空 dict，
    这里必须区分「解析失败」和「解析成功但确实没字幕」，否则会把网络故障
    误报成「这条视频没有字幕」。
    """
    try:
        from app import downloader

        info = downloader.extract_raw(url, cookie_file)
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__
    if not info:
        return None, "链接解析失败（网络不通 / 平台限制 / 链接无效）"
    return info, ""


_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.I)


def _is_bilibili(url) -> bool:
    low = (url or "").lower()
    return "bilibili.com" in low or "b23.tv" in low


# 平台串轨是随机行为，单次命中率约 1/3；多试几次能显著提高成功率，
# 一旦命中合格字幕会被缓存 30 分钟，后续不再重复请求。
BILI_SUBTITLE_ATTEMPTS = 10


def fetch_bilibili_direct(url, cookies_text="", on_progress=None) -> tuple:
    """B 站专用兜底：带登录态直连 x/player/v2 取 AI 字幕正文。

    实测平台会「串轨」：同一个 bvid + cid 连续请求，返回的字幕可能来自别的
    视频（时间跨度只有 0.25x / 甚至 1.6x 时长，主题完全无关）。直接拿去总结
    会产出一份看起来很正常、其实完全跑题的笔记。所以每份候选都要过
    subtitle_quality()，最多重试 6 次，只返回合格字幕；全部不合格就明确报错，
    绝不降级使用错字幕。

    返回 (info, error)，失败时 info 为 None。
    """
    m = _BV_RE.search(url or "")
    if not m:
        return None, "链接里没有识别到 BV 号"
    bvid = m.group(0)
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if cookies_text:
        headers["Cookie"] = cookies_text
    worst = None

    def progress(cur, hint):
        if on_progress:
            try:
                on_progress(cur, BILI_SUBTITLE_ATTEMPTS, hint)
            except Exception:  # noqa: BLE001
                pass

    try:
        with httpx.Client(timeout=12, follow_redirects=True) as client:
            view = client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid}, headers=headers,
            ).json()
            d = view.get("data") or {}
            cid = d.get("cid")
            if not cid:
                return None, f"取不到视频 cid（code={view.get('code')}）"
            title = d.get("title") or ""
            duration = d.get("duration") or 0
            for attempt in range(BILI_SUBTITLE_ATTEMPTS):
                progress(attempt + 1, "正在请求平台字幕接口")
                try:
                    v2 = client.get(
                        "https://api.bilibili.com/x/player/v2",
                        params={"bvid": bvid, "cid": cid}, headers=headers,
                    ).json()
                except (ValueError, httpx.RequestError):
                    v2 = {}
                subs = [s for s in (((v2.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
                        if s.get("subtitle_url")]
                if not subs:
                    time.sleep(0.3)
                    continue
                subs.sort(key=lambda s: 0 if str(s.get("lan") or "").lower().startswith(("zh", "ai-zh")) else 1)
                chosen = subs[0]
                sub_url = chosen["subtitle_url"]
                if sub_url.startswith("//"):
                    sub_url = "https:" + sub_url
                try:
                    raw = client.get(sub_url, headers=headers).text
                except httpx.RequestError:
                    raw = ""
                if not raw or not raw.lstrip().startswith("{"):
                    time.sleep(0.3)
                    continue
                q = subtitle_quality(parse_json3(raw), duration, title)
                if worst is None or q["score"] > worst["score"]:
                    worst = q
                if q["ok"]:
                    progress(attempt + 1, "字幕校验通过")
                    lang = chosen.get("lan") or "ai-zh"
                    return {
                        "title": title,
                        "duration": duration,
                        "subtitles": {
                            lang: [{"ext": "json3", "data": raw, "name": chosen.get("lan_doc") or lang}],
                        },
                    }, ""
                progress(attempt + 1, f"返回内容不匹配（只到 {q['end_text']}），已跳过")
                time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__

    if worst is None:
        return None, "平台没返回可用字幕轨"
    return None, (
        f"平台连续 {BILI_SUBTITLE_ATTEMPTS} 次返回的 AI 字幕都和这条视频对不上"
        f"（最好的一份只到 {worst['end_text']}，视频时长 {_mmss(duration)}）。"
        "这通常是 B 站临时限流，稍等半分钟再点一次 AI 总结；"
        "如果一直这样，试试在右上角导入完整登录信息后重试。"
    )


def _title_keywords(title) -> list:
    """从视频标题里提取主题词，用于判断字幕是不是这条视频的。

    B 站 AI 字幕接口会随机返回别的视频的字幕（串轨），光看时间跨度挡不住
    「覆盖刚好但内容完全不同」的情况，必须再看主题词是否出现在字幕全文里。
    """
    t = (title or "").lower()
    words = {w for w in re.findall(r"[a-z0-9]{4,}", t) if len(w) >= 4}
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        for i in range(len(chunk) - 1):
            words.add(chunk[i:i + 2])
    return sorted(w for w in words if len(w) >= 2)


def _title_strong_keywords(title) -> list:
    """标题里的「强主题词」：英文单词（≥4 字符）与中文 3-gram。

    中文 2-gram 里「这么 / 不如 / 为啥」太常见，串轨字幕随便就能命中，
    不足以证明字幕属于这条视频；3-gram 与英文专名才有区分度。
    """
    t = (title or "").lower()
    words = {w for w in re.findall(r"[a-z0-9]{4,}", t)}
    for chunk in re.findall(r"[\u4e00-\u9fff]{3,}", t):
        for i in range(len(chunk) - 2):
            words.add(chunk[i:i + 3])
    return sorted(words)


def subtitle_quality(segments, duration=0, title="") -> dict:
    """评估一份字幕是否为「这条视频的完整字幕」，返回可信度明细。

    串轨字幕的典型特征：时间跨度远小于或远大于视频时长、段密度异常、
    全文不含标题主题词。宁可拒绝并重试，也不能把它喂给 LLM 生成一份
    看起来很正常、其实完全跑题的笔记。
    """
    n = len(segments)
    last = float(segments[-1].get("start") or 0) if n else 0.0
    first = float(segments[0].get("start") or 0) if n else 0.0
    dur = float(duration or 0)
    ratio = (last / dur) if dur > 60 else 1.0
    span_min = max(0.0, last - first) / 60.0
    density = n / span_min if span_min > 0.5 else float(n)
    keywords = _title_keywords(title)
    strong = _title_strong_keywords(title)
    body = "".join(str(s.get("text") or "") for s in segments[:500]).lower()
    hits = sum(1 for w in keywords if w in body)
    strong_hits = sum(1 for w in strong if w in body)
    coverage_ok = 0.85 <= ratio <= 1.10
    density_ok = density >= 8
    if strong:
        # 有专名/3-gram 时，至少命中一个才算「这条视频的字幕」
        topic_ok = strong_hits >= 1
    elif keywords:
        need = 2 if len(keywords) >= 3 else 1
        topic_ok = hits >= need
    else:
        topic_ok = True
    ok = n >= 8 and coverage_ok and density_ok and topic_ok
    score = (
        100 - min(100, abs(1.0 - ratio) * 200)
        + min(n, 600) / 10.0
        + min(strong_hits, 4) * 30
        + min(hits, 5) * 5
        + (30 if ok else -60)
    )
    return {
        "ok": ok, "score": round(score, 1), "count": n,
        "end": last, "end_text": _mmss(last), "ratio": round(ratio, 3),
        "density": round(density, 1), "keywords": len(keywords), "hits": hits,
        "strong_hits": strong_hits,
        "coverage_ok": coverage_ok, "density_ok": density_ok, "topic_ok": topic_ok,
    }


def _known_no_subtitle(url) -> bool:
    """自有通道 / 图文平台本来就没有字幕轨，不该把解析失败误报成网络问题"""
    low = (url or "").lower()
    return any(d in low for d in ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com"))


def resolve(url, has_cookie=False, cookies_text="", cookie_file="", info=None,
            max_segments=2000, on_progress=None) -> dict:
    """取一条视频的字幕正文，统一返回结构（失败时 ok=False + 人话 reason）。"""
    cookie_fp = hashlib.md5((cookies_text or "").encode("utf-8")).hexdigest()[:8] if cookies_text else ""
    resolved_key = _key(url, has_cookie, cookie_fp)
    hit = _resolved_cache.get(resolved_key)
    if hit and time.time() - hit["ts"] < config.SUBTITLE_TTL:
        return hit["data"]

    load_error = ""
    direct_error = ""
    direct_tried = False
    source = "manual" if info is not None else "cache"
    if info is None:
        info = cache_get(url, has_cookie)
        if info is None:
            source = "yt-dlp"
            # B 站带登录态时优先直连：yt-dlp 的 wbi 接口常 412，别白跑一次解析
            if _is_bilibili(url) and cookies_text:
                direct_tried = True
                direct, direct_error = fetch_bilibili_direct(url, cookies_text, on_progress)
                if direct:
                    info = direct
                    source = "bilibili-direct"
            if info is None:
                info, load_error = _load_info(url, cookie_file)
                if info:
                    cache_put(url, has_cookie, info)
    info = info or {}

    # parse() 不再取字幕正文，缓存里的 info 可能没有轨道；再兜底一次直连
    if not track_list(info) and _is_bilibili(url) and cookies_text and not direct_tried:
        direct_tried = True
        direct, direct_error = fetch_bilibili_direct(url, cookies_text, on_progress)
        if direct:
            info = direct
            source = "bilibili-direct"
            load_error = ""

    tracks = track_list(info)
    result = {
        "ok": False,
        "url": url,
        "title": info.get("title") or "",
        "duration": info.get("duration") or 0,
        "source": source,
        "load_error": load_error,
        "track_count": len(tracks),
        "tracks": [
            {"lang": t["lang"], "name": t["name"], "auto": t["auto"], "ext": t["ext"], "has_data": bool(t["data"])}
            for t in tracks
        ],
    }

    picked = pick_track(tracks)
    if not picked:
        if direct_error and has_cookie:
            # 直连已经给出了带「下一步」的人话，别再套一层含 Cookie 字样的包装
            # （前端看到 cookie 字样会自动弹导入弹窗，反而误导用户）
            result["reason"] = direct_error
        elif load_error and not _known_no_subtitle(url):
            result["reason"] = f"暂时没法判断这条视频有没有字幕：{load_error}。请检查网络或换个链接重试"
        else:
            result["reason"] = no_subtitle_hint(url, has_cookie)
        return result

    raw = picked.get("data") or ""
    if not raw and picked.get("url"):
        try:
            raw = _fetch_url(picked["url"], cookies_text, _referer_for(url))
        except Exception as e:  # noqa: BLE001
            result["reason"] = f"字幕地址取不到内容（{type(e).__name__}），可能是链接失效或需要登录态 Cookie"
            return result
    if not raw:
        result["reason"] = no_subtitle_hint(url, has_cookie)
        return result

    # HLS 字幕（TED 等）：字幕 URL 指向 m3u8 播放列表，需并发取回所有 vtt 分片再合并
    if raw.lstrip().startswith("#EXTM3U"):
        raw = _fetch_hls(raw, picked["url"], cookies_text, _referer_for(url))
        if not raw:
            result["reason"] = "字幕播放列表里的分片没拉到内容，可能是网络限制，稍后重试"
            return result

    segments = parse_subtitle(raw, picked["ext"])
    # HLS 多分片合并后时间码可能有重叠/错序，统一按起始时间排好
    segments.sort(key=lambda s: s.get("start") or 0)
    if not segments:
        result["reason"] = "字幕文件读到了，但没解析出有效文本，换一条带人工字幕的视频试试"
        return result

    # 质量校验必须在截断前做，否则长字幕被 max_segments 截掉尾部会误判覆盖不足
    quality = subtitle_quality(segments, result["duration"], result["title"])
    result.update({
        "quality": quality,
        "subtitle_end": quality["end"],
        "subtitle_end_text": quality["end_text"],
        "coverage": quality["ratio"],
    })
    if not quality["ok"] and source == "bilibili-direct":
        result["reason"] = (
            f"平台这次返回的 AI 字幕和这条视频对不上（字幕只到 {quality['end_text']}，"
            f"视频时长 {_mmss(result['duration'])}），已丢弃这份字幕，避免生成跑题笔记。"
            "稍等半分钟再点一次 AI 总结即可。"
        )
        return result

    segments = segments[:max_segments]
    text = "\n".join(f"[{_mmss(s['start'])}] {s['text']}" for s in segments)
    result.update({
        "ok": True,
        "lang": picked["lang"],
        "auto": picked["auto"],
        "ext": picked["ext"],
        "segment_count": len(segments),
        "chars": len(text),
        "text": text,
        "segments": segments,
    })
    # 不合格的字幕绝不缓存：否则一次串轨结果会被锁死 30 分钟，用户怎么重试都没用
    if quality["ok"]:
        with _lock:
            if len(_resolved_cache) >= _RESOLVED_MAX:
                oldest = min(_resolved_cache, key=lambda k: _resolved_cache[k]["ts"])
                _resolved_cache.pop(oldest, None)
            _resolved_cache[resolved_key] = {"ts": time.time(), "data": result}
    return result
