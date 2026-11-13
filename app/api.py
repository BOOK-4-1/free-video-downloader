"""API：解析 / 创建下载任务 / 查进度 / 下载文件 / 缩略图代理

刻意保持“轻”：
- 无数据库：任务态在内存（app/tasks.py），文件在 runtime/downloads，TTL 到期自动删；
- 无账号体系：付费能力本期只做前端页面（用户已确认），接口层不设权限。
"""
import asyncio
import io
import json
import os
import threading
import time
import uuid
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Body, HTTPException, Query
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app import config, downloader, subtitle, summarizer, tasks

router = APIRouter(prefix="/api")


class ParseReq(BaseModel):
    url: str
    cookies: str = ""


class DownloadReq(BaseModel):
    url: str
    format_id: str = "best"
    cookies: str = ""
    title: str = ""
    thumbnail: str = ""
    info: dict | None = None


class BatchItem(BaseModel):
    url: str
    title: str = ""


class BatchReq(BaseModel):
    items: list[BatchItem]
    format_id: str = "best"
    cookies: str = ""
    group_title: str = ""


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "yt_dlp": downloader.YT_DLP_VERSION,
        "ffmpeg": bool(config.FFMPEG_DIR),
        "ffmpeg_dir": config.FFMPEG_DIR or "",
        "file_ttl": config.FILE_TTL,
        "concurrency": config.MAX_CONCURRENT_TASKS,
        "disk_free": downloader.disk_free_text(),
        "time": int(time.time()),
    }


def _ck(cookies: str, url: str) -> str:
    """把前端粘贴的 Cookie 落成临时文件（按 URL 的平台选域名），空则返回空串"""
    text = (cookies or "").strip()
    if not text:
        return ""
    return downloader.write_cookiefile(text, downloader.platform_keys_of(url)) or ""


class CookieReq(BaseModel):
    cookies: str = ""
    url: str = ""


@router.post("/cookies/verify")
async def verify_cookies(req: CookieReq = Body(...)):
    """校验用户粘贴的 Cookie 是否真的能用，返回可读的结论。

    - 只粘贴 Cookie：本地字段校验（是否含登录态字段、B 站是否疑似游客态）
    - 同时给 url：真解析一次 + 与匿名结果对比；B 站还会问一次 nav 的 isLogin，
      挡住「游客态也能解析出 360P/480P」造成的假阳性（stage=guest）
    - 登录态接口不通时只给 warning，不判用户 Cookie 无效
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: downloader.verify_cookie(req.cookies, req.url))
    return {"success": True, "data": result}


@router.post("/parse")
async def parse(req: ParseReq = Body(...)):
    """解析链接：返回视频信息 + 可用清晰度档位。支持一次粘贴多段分享文案。

    合集 / 多 P 链接走 parse_playlist（只取清单，快），单条链接走 parse。
    """
    urls = downloader.extract_urls(req.url)
    if not urls:
        raise HTTPException(400, "没有识别到有效链接，请粘贴完整的视频地址")
    url = urls[0]
    loop = asyncio.get_running_loop()

    async def run(fn):
        return await asyncio.wait_for(
            loop.run_in_executor(None, fn), timeout=downloader.PARSE_TIMEOUT
        )

    try:
        if downloader.looks_like_playlist(url):
            # 收藏夹 / 空间页 / playlist 链接：直接走合集解析（否则 yt-dlp 会先吐一条无 formats 的错误）
            data = await run(lambda: downloader.parse_playlist(url, _ck(req.cookies, url)))
        else:
            data = await run(lambda: downloader.parse(url, _ck(req.cookies, url)))
    except downloader.ParseError as e:
        # 抖音走自有通道，不需要（也不该）再退一步试合集模式
        if downloader.douyin.is_douyin(url):
            raise HTTPException(400, str(e)) from e
        # 单条解析失败时退一步试合集模式：收藏夹 / 空间页直接贴进来也能用
        try:
            pl = await run(lambda: downloader.parse_playlist(url, _ck(req.cookies, url)))
            if not pl.get("items"):
                raise HTTPException(400, str(e)) from e
            data = pl
        except asyncio.TimeoutError:
            raise HTTPException(504, "解析超时，该平台可能被网络限制，换个链接试试") from None
        except downloader.ParseError:
            raise HTTPException(400, str(e)) from e
    except asyncio.TimeoutError:
        raise HTTPException(504, "解析超时，该平台可能被网络限制，换个链接试试") from None
    data["url"] = url
    return {"success": True, "data": data}


@router.post("/download")
async def create_download(req: DownloadReq = Body(...)):
    """创建服务端下载任务，立即返回 task_id，前端轮询进度"""
    urls = downloader.extract_urls(req.url)
    if not urls:
        raise HTTPException(400, "没有识别到有效链接")
    info = req.info or {}
    if not info.get("title"):
        info = {**info, "title": req.title}
    running = [t for t in tasks.all_tasks() if t["status"] in ("queued", "downloading", "merging")]
    if len(running) >= config.MAX_CONCURRENT_TASKS * 4:
        raise HTTPException(429, "当前下载人数较多，请稍后再试")
    task = tasks.create(urls[0], req.format_id, info)
    if req.cookies.strip():
        task["_cookies"] = req.cookies.strip()
    tasks.start(task["id"], req.cookies.strip())
    return {"success": True, "data": tasks.serialize(task)}


@router.post("/download/batch")
async def create_batch(req: BatchReq = Body(...)):
    """批量下载：一次提交合集 / 多 P 的多条链接，返回同一 group_id 下的任务列表。

    任务仍由同一个线程池排队执行（不额外加压平台），前端按条显示进度。
    """
    items = [it for it in req.items if downloader.extract_urls(it.url)]
    if not items:
        raise HTTPException(400, "没有识别到有效链接")
    if len(items) > config.BATCH_MAX:
        raise HTTPException(400, f"单次批量最多 {config.BATCH_MAX} 个视频")
    running = [t for t in tasks.all_tasks() if t["status"] in ("queued", "downloading", "merging")]
    if len(running) >= config.MAX_CONCURRENT_TASKS * 4:
        raise HTTPException(429, "当前下载人数较多，请稍后再试")

    group_id = uuid.uuid4().hex[:8]
    group_title = (req.group_title or "批量下载").strip()[:60]
    out = []
    for i, it in enumerate(items):
        meta = {
            "title": (it.title or "").strip() or f"第 {i + 1} 个视频",
            "group_id": group_id,
            "group_title": group_title,
            "group_index": i + 1,
            "group_count": len(items),
        }
        task = tasks.create(downloader.extract_urls(it.url)[0], req.format_id, meta)
        tasks.start(task["id"], req.cookies.strip())
        out.append(tasks.serialize(task))
    return {"success": True, "data": {"group_id": group_id, "group_title": group_title, "tasks": out}}


@router.get("/tasks")
async def list_tasks():
    return {"success": True, "data": [tasks.serialize(t) for t in tasks.all_tasks()[:50]]}


@router.get("/tasks/group/{group_id}")
async def get_group(group_id: str):
    """批量任务的整体进度：done / error / 总数 / 已占用空间"""
    group = [t for t in tasks.all_tasks() if t["group_id"] == group_id]
    if not group:
        raise HTTPException(404, "批量任务不存在或已过期")
    done = [t for t in group if t["status"] == "done"]
    failed = [t for t in group if t["status"] == "error"]
    total_size = sum(t["filesize"] for t in done)
    return {
        "success": True,
        "data": {
            "group_id": group_id,
            "group_title": group[0]["group_title"],
            "total": len(group),
            "done": len(done),
            "failed": len(failed),
            "finished": len(done) + len(failed) == len(group),
            "percent": round(sum(100 if t["status"] == "done" else t["percent"] for t in group) / len(group), 1),
            "filesize_text": downloader.fmt_size(total_size),
            "tasks": [tasks.serialize(t) for t in sorted(group, key=lambda x: x["group_index"])],
        },
    }


def _safe_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class _RemoveFile(BackgroundTask):
    """响应发完后删掉临时 zip，避免 runtime 里堆垃圾"""

    def __init__(self, path: str):
        super().__init__(_safe_unlink, path)


@router.get("/group/{group_id}/zip")
def download_group_zip(group_id: str):
    """把一批已完成的文件打包成 zip 落到 runtime/tmp，再用 FileResponse 发送。

    - 用普通 def（跑到线程池），不在事件循环里同步压 100MB+；
    - 落盘后再发，Content-Length 准确，浏览器才有真实进度；
    - 视频本身已压缩，ZIP_STORED 仅打包不重复压缩。
    """
    import tempfile
    import zipfile

    group = sorted(
        [t for t in tasks.all_tasks() if t["group_id"] == group_id],
        key=lambda x: x["group_index"],
    )
    done = [t for t in group if t["status"] == "done" and os.path.exists(t.get("filepath") or "")]
    if not done:
        raise HTTPException(404, "这批任务还没有已完成的文件")

    os.makedirs(config.RUNTIME_DIR, exist_ok=True)
    fd, zpath = tempfile.mkstemp(suffix=".zip", prefix=f"vg-{group_id}-", dir=config.RUNTIME_DIR)
    os.close(fd)
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
            for t in done:
                ext = os.path.splitext(t["filepath"])[1]
                arc = f"{t['group_index']:02d}-{downloader.safe_filename(t['title'], ext)}"
                zf.write(t["filepath"], arcname=arc)
        name = downloader.safe_filename(
            (group[0]["group_title"] if group else "batch") or "batch", ".zip"
        )
        return FileResponse(
            zpath,
            media_type="application/zip",
            filename=name,
            background=_RemoveFile(zpath),
        )
    except Exception:
        _safe_unlink(zpath)
        raise


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    t = tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在或已过期")
    return {"success": True, "data": tasks.serialize(t)}


@router.get("/file/{task_id}")
async def get_file(task_id: str, inline: bool = Query(False), name: str = Query("")):
    """下载成品文件；inline=1 时用于站内预览播放；name= 指定浏览器保存的文件名"""
    t = tasks.get(task_id)
    if not t or t["status"] != "done" or not t.get("filepath"):
        raise HTTPException(404, "文件不存在或已过期")
    path = t["filepath"]
    if not os.path.exists(path):
        raise HTTPException(404, "文件已被清理，请重新下载")
    ext = os.path.splitext(path)[1].lower()
    media = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".opus": "audio/opus",
    }.get(ext, "application/octet-stream")
    disp_type = "inline" if inline else "attachment"
    filename = name.strip() if name.strip() else (t.get("filename") or os.path.basename(path))
    return FileResponse(
        path,
        media_type=media,
        filename=filename,
        content_disposition_type=disp_type,
    )


# 取图失败时返回可见占位（原来的 1x1 透明像素只会让用户看到一块灰色，无法判断原因）。
# 不缓存：一旦图床恢复或策略调整，刷新即可生效。
_THUMB_EMPTY_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360">
<rect width="640" height="360" fill="#f1f5f9"/>
<g fill="none" stroke="#cbd5e1" stroke-width="5" stroke-linejoin="round">
<rect x="258" y="116" width="124" height="96" rx="12"/>
<circle cx="294" cy="146" r="9"/>
<path d="M266 202l34-34 26 26 22-20 26 28"/>
</g>
<text x="320" y="264" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#94a3b8">封面暂不可用</text>
</svg>""".encode("utf-8")


def _thumb_referers(url: str):
    """按图床域名选优先 Referer；第二个候选为空，覆盖「禁止带 Referer」的 CDN。"""
    host = (urlparse(url).hostname or "").lower()
    if "hdslb" in host or "bilibili" in host:
        return ["https://www.bilibili.com/", ""]
    if any(k in host for k in ("douyin", "snssdk", "ixigua", "bytecdntp")):
        return ["https://www.douyin.com/", ""]
    if any(k in host for k in ("xhscdn", "xiaohongshu")):
        return ["https://www.xiaohongshu.com/", ""]
    p = urlparse(url)
    return [f"{p.scheme}://{p.netloc}/", ""]


@router.get("/thumb")
async def thumb(url: str = Query(...)):
    """缩略图代理：绕过 B 站/抖音等图床的防盗链。

    yt-dlp 的 B 站 extractor 常返回 http:// 图床地址；实测服务进程内请求该 http
    地址稳定失败、换成 https 正常，因此 http 一律优先升级为 https，失败再回退原地址。
    每个候选再各试「带 Referer / 不带 Referer」，覆盖不同 CDN 的防盗链策略。
    """
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "无效的图片地址")

    candidates = []
    if url.startswith("http://"):
        candidates.append("https://" + url[7:])
    candidates.append(url)

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    last_err = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for candidate in candidates:
                for ref in _thumb_referers(url):
                    headers = dict(base_headers)
                    if ref:
                        headers["Referer"] = ref
                    try:
                        r = await client.get(candidate, headers=headers)
                        r.raise_for_status()
                        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                        # 有些 CDN 被拒时返回 200 + HTML，必须挡掉，否则前端还是碎图
                        if not ctype.startswith("image/") or len(r.content) < 100:
                            raise ValueError(f"非图片响应 {ctype} / {len(r.content)}B")
                        return Response(
                            content=r.content,
                            media_type=ctype,
                            headers={"Cache-Control": "public, max-age=3600"},
                        )
                    except Exception as e:  # noqa: BLE001
                        last_err = e
    except Exception as e:  # noqa: BLE001
        last_err = e

    print(f"[thumb] 取图失败 url={url[:120]} err={type(last_err).__name__}: {last_err}", flush=True)
    return Response(
        content=_THUMB_EMPTY_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# AI 视频总结：配置状态 / 字幕验证 / 幂等提交 / SSE 增量流
# ---------------------------------------------------------------------------
class AiSubtitleReq(BaseModel):
    url: str
    cookies: str = ""


@router.get("/ai/config")
async def ai_config():
    """只回配置态，绝不回 api_key；summarizer_ready=False 时前端给出配置指引"""
    cloud = bool(config.AI_BASE_URL and config.AI_API_KEY and config.AI_MODEL)
    fallback = bool(config.AI_FALLBACK_BASE_URL and config.AI_FALLBACK_MODEL)
    return {
        "success": True,
        "data": {
            "configured": cloud,
            "provider": "cloud" if cloud else ("local" if fallback else "none"),
            "model": config.AI_MODEL or config.AI_FALLBACK_MODEL or "",
            "has_fallback": fallback,
            "summarizer_ready": summarizer.is_ready(),
            "stage": "stable",
            "subtitle_cache": subtitle.cache_stats(),
        },
    }


@router.post("/ai/subtitle")
async def ai_subtitle(req: AiSubtitleReq = Body(...)):
    """调试/验证用：返回字幕轨道清单 + 归一化后的带时间戳分段（不调用任何 LLM）。

    正常流程里 /api/parse 已经把字幕正文放进内存缓存，这里直接命中；
    独立调用（没先解析）时会自己 extract_info 一次。
    """
    urls = downloader.extract_urls(req.url)
    if not urls:
        raise HTTPException(400, "没有识别到有效链接")
    url = urls[0]
    cookie_file = _ck(req.cookies, url)
    loop = asyncio.get_running_loop()
    try:
        data = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: subtitle.resolve(
                    url,
                    has_cookie=bool(cookie_file),
                    cookies_text=downloader.normalize_cookie_text(req.cookies),
                    cookie_file=cookie_file,
                ),
            ),
            timeout=downloader.PARSE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "取字幕超时，该平台可能被网络限制，换个链接试试") from None
    return {"success": True, "data": data}


class AiLookupReq(BaseModel):
    url: str
    cookies: str = ""
    style: str = "detailed"


@router.post("/ai/lookup")
async def ai_lookup(req: AiLookupReq = Body(...)):
    """只读探测：这条视频（同一登录态 + 同一风格）是否已有可复用的 AI 笔记。

    与 POST /api/ai/summary 的区别 —— 本接口**绝不创建任务、不调用模型、不落临时 Cookie 文件**，
    只供前端在「解析成功后」决定右栏是秒开缓存、接回进行中的任务，还是显示「一键生成」。
    Cookie 原文只用于算登录态指纹，走请求体，不进 URL / 日志 / 响应。
    """
    urls = downloader.extract_urls(req.url)
    if not urls:
        raise HTTPException(400, "没有识别到有效链接")
    norm_cookies = downloader.normalize_cookie_text(req.cookies)
    hit = summarizer.find_by_key(summarizer.cache_key(urls[0], norm_cookies, req.style))
    status = hit["status"] if hit else "none"
    found = status in ("queued", "running", "done")
    data = {
        "found": found,
        "status": status,
        "summarizer_ready": summarizer.is_ready(),
    }
    if found:
        data.update({
            "id": hit["id"],
            "percent": hit["percent"],
            "message": hit["message"],
            "partial": hit["partial"],
            "result": hit["result"] if status == "done" else None,
            # 前端 ago() 按毫秒算，服务端 _now() 是秒
            "at_ms": int((hit["finished_at"] or hit["created_at"]) * 1000),
        })
    return {"success": True, "data": data}


class AiSummaryReq(BaseModel):
    url: str
    cookies: str = ""
    style: str = "detailed"
    force: bool = False      # 「重新生成」按钮：跳过已有结果强制重算


@router.post("/ai/summary")
async def ai_summary(req: AiSummaryReq = Body(...)):
    """提交或复用 AI 总结任务（按「归一化 URL + 登录态指纹 + 风格」幂等）。

    命中 done：直接把 result 返回，0 次模型调用；
    命中 queued/running：返回同一个 task_id，前端用 SSE 续接，不重复扣费；
    未命中：新建任务并启动。force=True 时强制新建、覆盖索引。
    """
    urls = downloader.extract_urls(req.url)
    if not urls:
        raise HTTPException(400, "没有识别到有效链接")
    url = urls[0]
    norm_cookies = downloader.normalize_cookie_text(req.cookies)
    key = summarizer.cache_key(url, norm_cookies, req.style)

    # 乐观查询：命中就不写临时 Cookie 文件，也不占用并发额度
    hit = None if req.force else summarizer.find_by_key(key)
    if hit and hit["status"] in ("queued", "running", "done"):
        data = summarizer.serialize(hit)
        data["reused"] = True
        return {"success": True, "data": data}

    running = [t for t in summarizer.all_tasks() if t["status"] in ("queued", "running")]
    if len(running) >= 8:
        raise HTTPException(429, "AI 总结任务较多，请稍后再试")
    cookie_file = _ck(req.cookies, url)
    # acquire 在锁内二次确认，挡住双击 / 多标签页的并发重复提交
    task, reused = summarizer.acquire(
        url, norm_cookies, cookie_file, req.style,
        loop=asyncio.get_running_loop(), force=req.force,
    )
    if reused and cookie_file:
        try:
            os.remove(cookie_file)   # 并发撞车时这个临时文件用不上了
        except OSError:
            pass
    if not reused:
        summarizer.start(task["id"])
    data = summarizer.serialize(task)
    data["reused"] = reused
    return {"success": True, "data": data}


@router.get("/ai/summary/{task_id}/stream")
async def ai_summary_stream(task_id: str, after: int = 0):
    """SSE：把生成过程增量推给页面（stage / meta / token / partial / done / error）。

    断线重连时用 after 补发历史事件；任务已完成时只补发历史后立即结束。
    旧的轮询接口保留，作为流不可用时的回退通道。
    """
    if not summarizer.get(task_id):
        raise HTTPException(404, "AI 总结任务不存在或已过期")

    async def gen():
        async for ev in summarizer.stream_events(task_id, after):
            yield (
                f"id: {ev['id']}\n"
                f"event: {ev['event']}\n"
                f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/ai/summary/{task_id}")
async def ai_summary_status(task_id: str):
    t = summarizer.get(task_id)
    if not t:
        raise HTTPException(404, "AI 总结任务不存在或已过期")
    return {"success": True, "data": summarizer.serialize(t)}


@router.get("/stats")
async def stats():
    """首页展示的实时统计（比封面更诚实：数字来自真实服务状态）"""
    ts = tasks.all_tasks()
    return {
        "success": True,
        "data": {
            "extractors": len(__import__("yt_dlp").extractor.gen_extractor_classes()),
            "yt_dlp": downloader.YT_DLP_VERSION,
            "tasks_total": len(ts),
            "tasks_done": len([t for t in ts if t["status"] == "done"]),
            "file_ttl_min": config.FILE_TTL // 60,
            "disk_free": downloader.disk_free_text(),
        },
    }
