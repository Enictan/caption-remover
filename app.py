"""
Tru Studio Caption Remover.
Runs a local server and shows the UI in a native window (pywebview).
Falls back to the default browser if a native window isn't available.
Dev run:  python app.py      (CR_BROWSER=1 to force browser, NO_BROWSER=1 for headless)
"""
from __future__ import annotations

if __name__ == "__main__":
    # macOS: restart from inside a small app bundle so the Dock shows "Caption Remover"
    import macapp
    macapp.ensure_bundle()

import io
import os
import shutil
import sys
import threading
import uuid
import webbrowser

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import engine
import updater

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0.4"
NATIVE = False  # set when running inside the native window
_home = os.path.expanduser("~")
_vids = "Movies" if sys.platform == "darwin" else "Videos"
WORK = os.environ.get("CR_OUTPUT") or os.path.join(_home, _vids, "Caption Remover")
JOBS_DIR = os.path.join(WORK, ".work")
os.makedirs(JOBS_DIR, exist_ok=True)

app = FastAPI(title="Caption Remover")
videos: dict[str, dict] = {}   # id -> {"path", "info", "name"}
jobs: dict[str, engine.Job] = {}


def _jpeg(rgb, q=88, max_w=1600):
    h, w = rgb.shape[:2]
    if w > max_w:
        rgb = cv2.resize(rgb, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, q])
    return Response(buf.tobytes(), media_type="image/jpeg")


def _video(vid):
    v = videos.get(vid)
    if not v:
        raise HTTPException(404, "Video not found - please load it again")
    return v


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(APP_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    vid = uuid.uuid4().hex[:10]
    d = os.path.join(JOBS_DIR, vid)
    os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(file.filename or "video.mp4")[1] or ".mp4"
    path = os.path.join(d, "input" + ext)
    with open(path, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            f.write(chunk)
    try:
        info = engine.probe(path)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "That file doesn't look like a video ffmpeg can read.")
    videos[vid] = {"path": path, "info": info, "name": file.filename or "video"}
    return {"id": vid, "info": info, "name": file.filename}


@app.get("/api/frame/{vid}")
def frame(vid: str, t: float = 0.0):
    v = _video(vid)
    return _jpeg(engine.read_frame(v["path"], t, v["info"]))


@app.post("/api/detect/{vid}")
def detect(vid: str, body: dict):
    v = _video(vid)
    t = float(body.get("t", 0))
    rgb = engine.read_frame(v["path"], t, v["info"])
    return {"boxes": engine.detect_text(rgb, float(body.get("sensitivity", 0.5)))}


@app.post("/api/scan/{vid}")
def scan(vid: str, body: dict):
    """Sample frames across the clip to suggest where captions live."""
    v = _video(vid)
    info = v["info"]
    sens = float(body.get("sensitivity", 0.5))
    dur = max(info["duration"], 0.1)
    n = max(4, min(32, int(body.get("n", 8))))
    hits = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        rgb = engine.read_frame(v["path"], t, info)
        for b in engine.detect_text(rgb, sens):
            hits.append({"t": t, "box": b})
    return {"hits": hits}


@app.get("/api/thumbs/{vid}")
def thumbs(vid: str, n: int = 24, h: int = 90):
    """Filmstrip: n evenly spaced frames tiled into one horizontal JPEG."""
    import subprocess
    v = _video(vid)
    info = v["info"]
    n = max(4, min(60, n))
    dur = max(info["duration"], 0.1)
    cmd = [engine.FFMPEG, "-v", "error", "-i", v["path"],
           "-vf", f"fps={n}/{dur:.3f},scale=-2:{h},tile={n}x1", "-frames:v", "1",
           "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"]
    out = subprocess.run(cmd, capture_output=True, **engine._NOWIN).stdout
    if not out:
        raise HTTPException(500, "Could not make thumbnails")
    return Response(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/preview/{vid}")
def preview(vid: str, body: dict):
    v = _video(vid)
    s = engine.Settings.from_dict(body.get("settings", {}))
    t = float(body.get("t", 0))
    _, out, _ = engine.preview(v["path"], v["info"], s, t)
    return _jpeg(out, 90)


@app.post("/api/process/{vid}")
def process(vid: str, body: dict):
    v = _video(vid)
    for j in jobs.values():
        if j.state in ("queued", "running"):
            raise HTTPException(409, "Another video is still processing. Wait for it or cancel it first.")
    s = engine.Settings.from_dict(body.get("settings", {}))
    base = os.path.splitext(os.path.basename(v["name"]))[0]
    ext = ".mov" if s.fmt == "prores" else ".mp4"
    out = os.path.join(WORK, f"{base}_clean{ext}")
    k = 2
    while os.path.exists(out):
        out = os.path.join(WORK, f"{base}_clean_{k}{ext}")
        k += 1
    jid = uuid.uuid4().hex[:10]
    j = engine.Job(v["path"], out, v["info"], s)
    jobs[jid] = j
    j.start()
    return {"job": jid, "output": out}


@app.get("/api/status/{jid}")
def status(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Job not found")
    st = j.status()
    st["output"] = j.out if j.state == "done" else None
    st["filename"] = os.path.basename(j.out)
    return st


@app.post("/api/cancel/{jid}")
def cancel(jid: str):
    j = jobs.get(jid)
    if j:
        j.cancel()
    return {"ok": True}


@app.get("/api/download/{jid}")
def download(jid: str):
    j = jobs.get(jid)
    if not j or j.state != "done":
        raise HTTPException(404, "Not ready")
    return FileResponse(j.out, filename=os.path.basename(j.out))


def _open(path, reveal=False):
    import subprocess
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    elif os.name == "nt":
        if reveal:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # noqa
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])


@app.post("/api/reveal/{jid}")
def reveal(jid: str):
    j = jobs.get(jid)
    if j and j.state == "done":
        _open(j.out, reveal=True)
    return {"ok": True}


@app.post("/api/open/{jid}")
def open_output(jid: str):
    j = jobs.get(jid)
    if j and j.state == "done":
        _open(j.out)
    return {"ok": True}


@app.get("/api/system")
def system():
    return {"device": engine.device(), "output_dir": WORK, "native": NATIVE, "version": VERSION,
            "dev": bool(os.environ.get("CR_DEV")),
            "platform": sys.platform, "folder": "Finder" if sys.platform == "darwin" else "Explorer"}


@app.get("/api/update/check")
def update_check():
    try:
        return updater.check(VERSION)
    except Exception as e:  # offline etc. -> quietly no update
        return {"enabled": updater.enabled(), "available": False, "current": VERSION, "error": str(e)}


@app.post("/api/update/install")
def update_install():
    if not updater.enabled():
        raise HTTPException(400, "Updates aren't available in this copy")
    updater.install(VERSION)
    return updater.state


@app.get("/api/update/status")
def update_status():
    return updater.state


@app.post("/api/update/restart")
def update_restart():
    for j in jobs.values():
        if j.state in ("queued", "running"):
            raise HTTPException(409, "A video is still processing. Restart when it's finished.")
    threading.Timer(0.4, updater.restart).start()
    return {"ok": True}


@app.get("/api/video/{vid}")
def video_file(vid: str):
    v = _video(vid)
    return FileResponse(v["path"])


def _warm():
    try:
        engine.text_reader()
    except Exception as e:  # noqa
        print("Text detector failed to load:", e)


def _free_port(preferred=7860):
    import socket
    for p in [preferred] + list(range(7861, 7900)):
        with socket.socket() as so:
            try:
                so.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def _mac_identity():
    """Show 'Caption Remover' + our icon in the Dock/menu bar instead of 'Python'."""
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Caption Remover"
    except Exception:
        pass


def _mac_icon():
    try:
        from AppKit import NSApplication, NSImage
        icns = os.environ.get("CR_ICON")
        if icns and os.path.exists(icns):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icns))
    except Exception:
        pass


def main():
    global NATIVE
    import uvicorn
    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=_warm, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    if os.environ.get("NO_BROWSER"):
        print(f"Caption Remover running at {url}")
        server.run()
        return

    use_native = not os.environ.get("CR_BROWSER")
    webview = None
    if use_native:
        try:
            if sys.platform == "darwin":
                _mac_identity()
            import webview  # pywebview
        except Exception as e:  # noqa
            print("Native window unavailable, using browser:", e)
            webview = None

    if webview is None:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        print(f"\n  Caption Remover running at {url}\n  Outputs save to: {WORK}\n  Close this window to stop.\n")
        server.run()
        return

    NATIVE = True
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    import time
    for _ in range(100):  # wait for the server
        if server.started:
            break
        time.sleep(0.05)
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass
    webview.create_window("Caption Remover", url, width=1440, height=900, min_size=(1100, 700),
                          background_color="#0e0e10")
    if sys.platform == "darwin":
        _mac_icon()  # must run on the main thread, before the event loop starts
    webview.start(debug=bool(os.environ.get("CR_DEV")))  # dev: right-click > Reload / Inspect
    server.should_exit = True
    os._exit(0)


if __name__ == "__main__":
    main()
