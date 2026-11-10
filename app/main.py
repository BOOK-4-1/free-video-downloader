"""秒下 VideoGrab —— 万能视频下载站（FastAPI 单服务）

启动：.venv\\Scripts\\python.exe run.py   然后浏览器打开 http://127.0.0.1:8000
"""
import asyncio
import contextlib
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import router

CLEAN_INTERVAL = 120  # 每 2 分钟清理一次过期文件


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from app import summarizer, tasks

    async def cleaner():
        while True:
            await asyncio.sleep(CLEAN_INTERVAL)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, tasks.cleanup, config.FILE_TTL)
                await loop.run_in_executor(None, summarizer.cleanup, config.AI_NOTE_TTL)
            except Exception:  # noqa: BLE001
                pass

    task = asyncio.create_task(cleaner())
    yield
    task.cancel()


app = FastAPI(title="秒下 VideoGrab API", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(router)

STORE = os.path.join(config.WEB_DIR, "store")


def _html(path: str):
    if not os.path.isfile(path):
        raise HTTPException(404, "页面不存在")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/")
async def index():
    return _html(os.path.join(config.WEB_DIR, "index.html"))


@app.get("/download")
async def download_page():
    return _html(os.path.join(config.WEB_DIR, "download.html"))


@app.get("/store")
async def store_page():
    return _html(os.path.join(STORE, "index.html"))


@app.get("/favicon.ico")
async def favicon():
    p = os.path.join(config.WEB_DIR, "assets", "img", "favicon.svg")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(404)


app.mount("/assets", StaticFiles(directory=os.path.join(config.WEB_DIR, "assets")), name="assets")
