"""全局路径与运行配置（不用数据库，所有状态在内存 + 临时目录）"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
DOWNLOAD_DIR = os.path.join(RUNTIME_DIR, "downloads")
YTDLP_DIR = os.path.join(BASE_DIR, "yt-dlp")

# 临时文件保留时长（秒）：过期由后台清理线程删除
FILE_TTL = int(os.environ.get("FILE_TTL", 30 * 60))
# 单个任务最长下载时间（秒），防止卡死任务堆积
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", 20 * 60))
# 同时进行的下载任务上限
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 3))
# 单次批量下载最多提交多少个视频（合集 / 多 P）
BATCH_MAX = int(os.environ.get("BATCH_MAX", 20))


def find_ffmpeg_dir():
    """优先用项目内置 ffmpeg，其次系统 PATH，最后 imageio-ffmpeg 自带二进制"""
    builtin = os.path.join(BASE_DIR, "tools", "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(builtin):
        return os.path.dirname(builtin)
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    try:
        import imageio_ffmpeg

        return os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


FFMPEG_DIR = find_ffmpeg_dir()

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
