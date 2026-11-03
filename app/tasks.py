"""下载任务：内存任务表 + 线程池执行（不依赖数据库 / Redis）

一个任务 = 一次 yt-dlp 下载，进度通过 progress_hooks 实时回写内存，
前端轮询 /api/tasks/{id} 即可看到百分比、速度、剩余时间。
"""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from app import config, downloader

_tasks = {}          # task_id -> dict
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_TASKS, thread_name_prefix="dl")

MAX_TASKS_KEPT = 200   # 内存里最多保留多少条历史任务


def _now() -> float:
    return time.time()


def create(url: str, format_id: str = "", info: dict = None) -> dict:
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "url": url,
        "format_id": format_id or "best",
        "title": (info or {}).get("title") or "",
        "thumbnail": (info or {}).get("thumbnail") or "",
        "platform_name": (info or {}).get("platform_name") or "",
        "duration_text": (info or {}).get("duration_text") or "",
        "status": "queued",      # queued / downloading / merging / done / error
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "downloaded": "",
        "total": "",
        "filename": "",
        "filepath": "",
        "filesize": 0,
        "error": "",
        "created_at": _now(),
        "finished_at": 0,
        "logs": [],
        # 批量下载所属的分组（合集 / 多 P）：同一组共享 group_id 与组名
        "group_id": (info or {}).get("group_id") or "",
        "group_title": (info or {}).get("group_title") or "",
        "group_index": (info or {}).get("group_index") or 0,
        "group_count": (info or {}).get("group_count") or 0,
        "file_url": "",
    }
    with _lock:
        _tasks[task_id] = task
        _trim()
    return task


def get(task_id: str):
    return _tasks.get(task_id)


def all_tasks() -> list:
    with _lock:
        return sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)


def _trim():
    if len(_tasks) <= MAX_TASKS_KEPT:
        return
    done = sorted(
        (t for t in _tasks.values() if t["status"] in ("done", "error")),
        key=lambda t: t["created_at"],
    )
    for t in done[: len(_tasks) - MAX_TASKS_KEPT]:
        _tasks.pop(t["id"], None)


def _update(task_id: str, **kw):
    with _lock:
        t = _tasks.get(task_id)
        if t:
            t.update(kw)


def start(task_id: str, cookies: str = ""):
    _pool.submit(_run, task_id, cookies)


def _run(task_id: str, cookies: str = ""):
    task = get(task_id)
    if not task:
        return
    # 抖音走自有免登录直链通道（yt-dlp 的抖音提取器服务端用不了）
    if downloader.douyin.is_douyin(task["url"]):
        return _run_douyin(task_id, task)

    cookie_file = downloader.write_cookiefile(cookies, downloader.platform_keys_of(task["url"])) if cookies else ""

    def hook(d):
        if get(task_id) is None:
            raise downloader.yt_dlp.utils.DownloadCancelled("任务已被清理")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            percent = (done / total * 100) if total else 0
            _update(
                task_id,
                status="downloading",
                percent=round(min(percent, 99.9), 1),
                speed=(d.get("_speed_str") or "").strip(),
                eta=(d.get("_eta_str") or "").strip(),
                downloaded=downloader.fmt_size(done),
                total=downloader.fmt_size(total),
            )
        elif d.get("status") == "finished":
            _update(task_id, percent=99.9, status="merging", speed="", eta="")

    def postprocessor_hook(d):
        if d.get("status") == "started":
            _update(task_id, status="merging")

    opts = downloader._base_opts()
    opts.update(
        {
            "outtmpl": downloader.outtmpl_for(task_id),
            "progress_hooks": [hook],
            "postprocessor_hooks": [postprocessor_hook],
            "noplaylist": True,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "merge_output_format": downloader.merge_output_format(),
            "overwrites": True,
        }
    )
    if cookie_file:
        opts["cookiefile"] = cookie_file

    # 编码偏好：默认 H.264（avc1），平台没有时自动兜底，见 _build_format_selector
    opts["format"] = downloader._build_format_selector(task["format_id"])

    _update(task_id, status="downloading")
    try:
        with downloader.yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(task["url"], download=True)
        path = ""
        reqs = (info or {}).get("requested_downloads") or []
        if reqs:
            path = reqs[0].get("filepath") or ""
        if not path or not os.path.exists(path):
            base = os.path.join(config.DOWNLOAD_DIR, task_id)
            for name in os.listdir(config.DOWNLOAD_DIR):
                if name.startswith(task_id + "."):
                    path = os.path.join(config.DOWNLOAD_DIR, name)
                    break
            if not path:
                raise RuntimeError("下载完成但没有找到文件")
        size = os.path.getsize(path)
        # 下载链接带上原始标题，浏览器保存时就是「标题.mp4」而不是 taskid.mp4
        final_title = (info or {}).get("title") or task["title"]
        base_name = downloader.safe_filename(final_title, os.path.splitext(path)[1])
        _update(
            task_id,
            status="done",
            percent=100,
            filepath=path,
            filename=os.path.basename(path),
            filesize=size,
            total=downloader.fmt_size(size),
            title=final_title,
            file_url=f"/api/file/{task_id}?name={quote(base_name)}",
            finished_at=_now(),
            speed="",
            eta="",
        )
    except Exception as e:  # noqa: BLE001
        msg = downloader._friendly_error(f"{type(e).__name__}: {e}")
        _update(task_id, status="error", error=msg, finished_at=_now(), speed="", eta="")
    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.remove(cookie_file)
            except OSError:
                pass


def _run_douyin(task_id: str, task: dict):
    """抖音：直链下载（单个 mp4，无需合并、无需 ffmpeg）"""
    _update(task_id, status="downloading")
    dest = os.path.join(config.DOWNLOAD_DIR, f"{task_id}.mp4")

    def on_progress(done, total, speed, eta):
        percent = (done / total * 100) if total else 0
        _update(
            task_id,
            percent=round(min(percent, 99.9), 1),
            speed=speed,
            eta=eta,
            downloaded=downloader.fmt_size(done),
            total=downloader.fmt_size(total),
        )

    def cancelled():
        return get(task_id) is None

    try:
        downloader.douyin.download(
            task["url"], task["format_id"], dest, on_progress=on_progress, cancel=cancelled
        )
        size = os.path.getsize(dest)
        base_name = downloader.safe_filename(task["title"] or "抖音视频", ".mp4")
        _update(
            task_id,
            status="done",
            percent=100,
            filepath=dest,
            filename=os.path.basename(dest),
            filesize=size,
            total=downloader.fmt_size(size),
            file_url=f"/api/file/{task_id}?name={quote(base_name)}",
            finished_at=_now(),
            speed="",
            eta="",
        )
    except Exception as e:  # noqa: BLE001
        msg = downloader._friendly_error(f"{type(e).__name__}: {e}")
        _update(task_id, status="error", error=msg, finished_at=_now(), speed="", eta="")


def serialize(task: dict) -> dict:
    """给前端的精简结构（隐藏服务器绝对路径）"""
    out = {k: v for k, v in task.items() if k not in ("filepath", "logs")}
    out["expires_in"] = (
        max(0, int(config.FILE_TTL - (_now() - task["finished_at"]))) if task["status"] == "done" else 0
    )
    return out


def cleanup(max_age: int):
    downloader.cleanup_downloads(max_age)
    downloader.cleanup_temp_cookies()
    with _lock:
        for tid, t in list(_tasks.items()):
            if t["finished_at"] and _now() - t["finished_at"] > max_age * 2:
                _tasks.pop(tid, None)
