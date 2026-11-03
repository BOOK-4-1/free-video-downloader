import json, os, sys, urllib.request, zipfile, time

dest = os.path.dirname(os.path.abspath(__file__))
wheel_dir = os.path.join(dest, "wheels")
os.makedirs(wheel_dir, exist_ok=True)

meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/imageio-ffmpeg/json", timeout=30))
url = next(u["url"] for u in meta["urls"] if u["filename"].endswith("win_amd64.whl"))
name = url.rsplit("/", 1)[-1]
target = os.path.join(wheel_dir, name)
if not os.path.exists(target) or os.path.getsize(target) < 30_000_000:
    print("downloading", url, flush=True)
    with urllib.request.urlopen(url, timeout=60) as r, open(target, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done % (4 * 1024 * 1024) < 256 * 1024:
                print(f"  {done/1048576:.1f}/{total/1048576:.1f} MB", flush=True)
print("wheel:", target, os.path.getsize(target), flush=True)

out_dir = os.path.join(dest, "ffmpeg")
os.makedirs(out_dir, exist_ok=True)
with zipfile.ZipFile(target) as z:
    for n in z.namelist():
        base = os.path.basename(n)
        if base.lower().startswith("ffmpeg") and base.lower().endswith((".exe", "")) and "bin" in n:
            data = z.read(n)
            p = os.path.join(out_dir, "ffmpeg.exe")
            with open(p, "wb") as f:
                f.write(data)
            os.chmod(p, 0o755)
            print("extracted:", p, len(data), flush=True)
print("DONE", flush=True)
