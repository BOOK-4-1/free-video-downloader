"""开发/生产共用启动脚本：python run.py [--port 8000]"""
import argparse
import os
import socket
import sys

import uvicorn


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    port = args.port
    if not args.reload and free_port(port) != port:
        port = free_port(port)
        print(f"[提示] 端口 {args.port} 被占用，改用 {port}")

    print(f"\n  秒下 VideoGrab 已启动 →  http://{args.host}:{port}\n", flush=True)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    sys.exit(main())
