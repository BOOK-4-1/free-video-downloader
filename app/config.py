"""全局路径与运行配置（不用数据库，所有状态在内存 + 临时目录）"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
DOWNLOAD_DIR = os.path.join(RUNTIME_DIR, "downloads")
YTDLP_DIR = os.path.join(BASE_DIR, "yt-dlp")


def _load_dotenv():
    """极简 .env 加载：只读项目根 .env，不覆盖已有环境变量，零新增依赖。

    只支持 KEY=VALUE 与 # 注释；值两侧的引号会被去掉。系统环境变量优先级更高，
    方便部署时用真实环境变量临时覆盖，而不必改文件。
    """
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

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

# --- AI 视频总结（一期只做字幕，不接语音转写）---------------------------------
# 主端点走 OpenAI Chat Completions 兼容协议，可指向任意供应商 / 任意模型；
# 主端点不可用时自动切到本地兜底（Ollama / LM Studio 等同样兼容的端点）。
# 密钥只从服务端环境变量或 .env 读取，绝不下发到浏览器。
AI_BASE_URL = os.environ.get("AI_BASE_URL", "").rstrip("/")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "")
AI_FALLBACK_BASE_URL = os.environ.get("AI_FALLBACK_BASE_URL", "").rstrip("/")
AI_FALLBACK_MODEL = os.environ.get("AI_FALLBACK_MODEL", "")
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", 120))
AI_MAX_CHARS = int(os.environ.get("AI_MAX_CHARS", 12000))
AI_MAX_SEGMENTS = int(os.environ.get("AI_MAX_SEGMENTS", 12))

# 字幕正文缓存：/api/parse 已经付过字幕下载成本，缓存起来给 AI 总结直接复用
SUBTITLE_TTL = int(os.environ.get("SUBTITLE_TTL", 30 * 60))
SUBTITLE_CACHE_MAX = int(os.environ.get("SUBTITLE_CACHE_MAX", 50))

# AI 笔记结果保留时长（秒）：同一视频重复打开直接复用，不再重复调用大模型。
# 只存服务端内存、不落盘；服务重启后自然失效，与「无数据库」架构一致。
AI_NOTE_TTL = int(os.environ.get("AI_NOTE_TTL", FILE_TTL * 2))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
