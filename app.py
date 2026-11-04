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
import time
import uuid
import webbrowser

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import engine
import updater
import voice

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.7.0"
NATIVE = False  # set when running inside the native window
_home = os.path.expanduser("~")
_vids = "Movies" if sys.platform == "darwin" else "Videos"
DEFAULT_OUT = os.environ.get("CR_OUTPUT") or os.path.join(_home, _vids, "Caption Remover")
SUPPORT = os.environ.get("CR_SUPPORT") or os.path.join(_home, ".caption-remover")
SETTINGS_FILE = os.path.join(SUPPORT, "settings.json")
if sys.platform == "darwin":
    LOG_FILE = os.path.join(_home, "Library", "Logs", "Caption Remover.log")
elif os.name == "nt":
    LOG_FILE = os.path.join(SUPPORT, "log.txt")
else:
    LOG_FILE = None
JOBS_DIR = os.path.join(DEFAULT_OUT, ".work")
os.makedirs(JOBS_DIR, exist_ok=True)
WINDOW = None   # native window (for folder picker)

DEFAULT_SETTINGS = {
    "output_dir": DEFAULT_OUT,
    "default_mode": "quality",      # fast | quality
    "default_fmt": "mp4",           # mp4 | prores
    "auto_update": True,
    "precise": True,                # letter-shaped masks + caption plates
    "temporal": True,               # borrow background from nearby frames
    "stabilise": True,
    "detect_every": 3,
    "stem_model": "htdemucs",       # htdemucs | htdemucs_6s | htdemucs_ft | roformer
    "keys_open": False,             # Music: on-screen keyboard shown
    "keys_names": True,             # note names on the keys
    "keys_scale": "sel",            # highlight a deck's key: off | sel | A | B
    "keys_octave": 3,               # lowest octave shown
    "keys_type": False,             # play notes from the computer keyboard
    "keys_vol": 0.6,
    "quantize": True,               # Music: hot cues, loops and jumps snap to the beat
}


def load_settings():
    import json
    st = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            st.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    if not st.get("output_dir"):
        st["output_dir"] = DEFAULT_OUT
    return st


def save_settings(st):
    import json
    os.makedirs(SUPPORT, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def out_dir():
    d = load_settings()["output_dir"]
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        os.makedirs(DEFAULT_OUT, exist_ok=True)
        return DEFAULT_OUT


WORK = DEFAULT_OUT   # kept for older code paths
voice.init(JOBS_DIR, out_dir)

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
    WORK = out_dir()
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
    return {"device": engine.device(), "output_dir": out_dir(), "native": NATIVE, "version": VERSION,
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
    for j in voice.jobs.values():
        if j.state == "running":
            raise HTTPException(409, "A voice task is still running. Restart when it's finished.")
    voice.stop()
    threading.Timer(0.4, updater.restart).start()
    return {"ok": True}


# --------------------------------------------------------------------------- music: song matcher
MUSIC_DIR = os.path.join(JOBS_DIR, "music")
os.makedirs(MUSIC_DIR, exist_ok=True)


def _clean_music_work(prefixes=("pcm_", "stem_", "full_", "pv_"), older_than=12 * 3600):
    """Playback copies are rebuilt on demand: drop old ones so they don't fill the disk."""
    import time
    now = time.time()
    for n in os.listdir(MUSIC_DIR):
        p = os.path.join(MUSIC_DIR, n)
        try:
            if n.startswith(prefixes) and now - os.path.getmtime(p) > older_than:
                os.remove(p)
        except OSError:
            pass


_clean_music_work()
tracks: dict[str, dict] = {}     # id -> analysis + path + name


def _track(tid):
    t = tracks.get(tid)
    if not t:
        raise HTTPException(404, "Song not found. Please add it again.")
    return t


SONG_MEM_DIR = os.path.join(SUPPORT, "songs")


def _mem_path(t):
    return os.path.join(SONG_MEM_DIR, t["cid"] + ".json")


def load_mem(t) -> dict:
    """What you set up for a song (hot cues, key fix, tempo x2/x½), remembered by the file's content."""
    import json
    try:
        with open(_mem_path(t), encoding="utf-8") as f:
            m = json.load(f)
            return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def save_mem(t, **changes):
    import json
    m = load_mem(t)
    m.update(changes)
    os.makedirs(SONG_MEM_DIR, exist_ok=True)
    tmp = _mem_path(t) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f)
    os.replace(tmp, _mem_path(t))
    t["mem"] = m
    return m


def _apply_key(t, tonic, mode):
    import music
    t.update(tonic=tonic, mode=mode, key=music.key_name(tonic, mode), camelot=music.camelot(tonic, mode), key_conf=1.0,
             key_user=True, key_source="you")
    rt, rm = music._relative(tonic, mode)
    t["key_alts"] = [{"tonic": rt, "mode": rm, "key": music.key_name(rt, rm) + " (same notes)", "camelot": music.camelot(rt, rm)}]


def _public(t):
    out = {k: v for k, v in t.items() if k not in ("path", "stems", "_peaks")}
    out["stem_names"] = list((t.get("stems") or {}).keys())
    return out


@app.post("/api/music/upload")
async def music_upload(file: UploadFile = File(...)):
    import music
    tid = uuid.uuid4().hex[:10]
    ext = os.path.splitext(file.filename or "song.mp3")[1] or ".mp3"
    path = os.path.join(MUSIC_DIR, tid + ext)
    with open(path, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            f.write(chunk)
    try:
        import asyncio
        a = await asyncio.get_running_loop().run_in_executor(None, music.analyse, path)
        a["wave"] = await asyncio.get_running_loop().run_in_executor(None, music.waveform, path, 600)
    except Exception as e:  # noqa
        os.remove(path)
        raise HTTPException(400, f"Couldn't read that audio file ({e})")
    name = os.path.splitext(os.path.basename(file.filename or "Song"))[0]
    if a.get("title"):
        name = a["title"] + (f" · {a['artist']}" if a.get("artist") else "")
    t = dict(a, id=tid, name=name, path=path, detected={"tonic": a["tonic"], "mode": a["mode"]}, cid=music.content_id(path))
    tracks[tid] = t
    try:
        st = load_settings().get("stem_model", "htdemucs")
        hit = music.cached_stems(os.path.join(SUPPORT, "stems"), path, st)
        if hit:
            t["stems"], t["stems_model"] = hit, st
            t.update(music.key_from_stems(hit, t["beats"]))
            _stem_jobs[tid] = {"state": "done", "progress": 1.0, "message": "Done", "model": st}
    except Exception:
        pass
    t["mem"] = load_mem(t)
    k = t["mem"].get("key")
    if isinstance(k, dict) and "tonic" in k:
        _apply_key(t, int(k["tonic"]) % 12, k.get("mode", "major"))
    return _public(t)


@app.post("/api/music/mem/{tid}")
def music_mem(tid: str, body: dict):
    t = _track(tid)
    ok = {k: v for k, v in body.items() if k in ("hot", "bpm_factor", "eq")}
    return save_mem(t, **ok)


@app.post("/api/music/key/{tid}")
def music_set_key(tid: str, body: dict):
    import music
    t = _track(tid)
    tonic, mode = int(body["tonic"]) % 12, body["mode"]
    _apply_key(t, tonic, mode)
    try:
        save_mem(t, key={"tonic": tonic, "mode": mode})
    except Exception:
        pass
    return _public(t)


@app.post("/api/music/match")
def music_match(body: dict):
    import music
    main = _track(body["main"])
    out = []
    for cid in body.get("cands", []):
        c = tracks.get(cid)
        if c:
            m = music.match(main, c)
            m["id"] = cid
            m["result"] = music.shifted_key(c, m["shift"])
            out.append(m)
    out.sort(key=lambda m: -m["fit"])
    return {"matches": out}


@app.post("/api/music/preview")
def music_preview(body: dict):
    """Render a beat-aligned window of the main song and the candidate (transposed + tempo-matched)."""
    import music
    main, cand = _track(body["main"]), _track(body["cand"])
    length = float(body.get("length", 30))
    semis = int(body.get("semis", 0))
    ratio = float(body.get("tempo_ratio", 1.0)) if body.get("match_tempo", True) else 1.0
    ms = music.nearest_beat(main["beats"], float(body.get("main_start", 0)))
    ms = min(ms, max(0.0, main["duration"] - length))
    cs = music.nearest_beat(cand["beats"], float(body.get("cand_start", 0)))
    cs = min(cs, max(0.0, cand["duration"] - length * ratio))
    key = f"{main['id']}_{ms:.2f}_{length:.0f}"
    mpath = os.path.join(MUSIC_DIR, f"pv_main_{key}.wav")
    if not os.path.exists(mpath):
        music.render(main["path"], mpath, ms, length)
    cpath = os.path.join(MUSIC_DIR, f"pv_c_{cand['id']}_{cs:.2f}_{semis}_{ratio:.4f}_{length:.0f}.wav")
    if not os.path.exists(cpath):
        music.render(cand["path"], cpath, cs, length, semis, ratio)
    return {"main_url": "/api/music/file/" + os.path.basename(mpath), "cand_url": "/api/music/file/" + os.path.basename(cpath),
            "main_start": ms, "cand_start": cs, "length": length,
            "result": music.shifted_key(cand, semis), "bpm_after": round(cand["bpm"] * ratio, 1)}


@app.get("/api/music/file/{name}")
def music_file(name: str):
    p = os.path.join(MUSIC_DIR, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="audio/wav")


@app.post("/api/music/export")
def music_export(body: dict):
    """Save the whole candidate song transposed (and tempo-matched) as a WAV in the output folder."""
    import music
    cand = _track(body["cand"])
    semis = int(body.get("semis", 0))
    ratio = float(body.get("tempo_ratio", 1.0)) if body.get("match_tempo", True) else 1.0
    tag = []
    if semis:
        tag.append(f"{semis:+d} st")
    if abs(ratio - 1) > 1e-4:
        tag.append(f"{cand['bpm'] * ratio:.1f} BPM")
    tag.append(music.shifted_key(cand, semis)["key"])
    safe = "".join(ch for ch in cand["name"] if ch not in '\\/:*?"<>|').strip() or "song"
    out = os.path.join(out_dir(), f"{safe} ({', '.join(tag)}).wav")
    music.render(cand["path"], out, 0, None, semis, ratio, fade=False)
    return {"output": out, "filename": os.path.basename(out)}


@app.post("/api/music/reveal")
def music_reveal(body: dict):
    p = body.get("path")
    root = os.path.abspath(out_dir())
    if p and os.path.exists(p) and os.path.commonpath([os.path.abspath(p), root]) == root:
        _open(p, reveal=True)
    return {"ok": True}


# ---- decks: full song, waveform, stems, save
STEMS_CACHE = os.path.join(SUPPORT, "stems")
_stem_pool = None
_stem_jobs: dict[str, dict] = {}


@app.get("/api/music/audio/{tid}")
def music_audio(tid: str, wav: int = 0):
    t = _track(tid)
    if wav:   # for formats the window can't decode itself
        import music
        p = os.path.join(MUSIC_DIR, f"full_{tid}.wav")
        if not os.path.exists(p):
            music.render(t["path"], p, 0, None, 0, 1.0, fade=False)
        return FileResponse(p, media_type="audio/wav")
    return FileResponse(t["path"])


@app.get("/api/music/pcm/{tid}")
def music_pcm(tid: str, sr: int = 44100, stem: str = ""):
    """The song (or one stem) as plain WAV at exactly the playback device's sample rate, so the window
    never decodes a compressed file or resamples itself (WebKit can get either wrong: wrong speed)."""
    import hashlib
    import soundfile as sf
    import music
    t = _track(tid)
    src = (t.get("stems") or {}).get(stem) if stem else t["path"]
    if not src or not os.path.exists(src):
        raise HTTPException(404, "Audio not found")
    sr = int(sr) if 8000 <= int(sr) <= 192000 else music.SR
    key = hashlib.sha1(f"{src}|{os.path.getmtime(src)}|{sr}".encode()).hexdigest()[:12]
    w = os.path.join(MUSIC_DIR, f"pcm_{tid}_{stem or 'mix'}_{key}.wav")
    if not os.path.exists(w):
        a = music.audio(src)
        if sr != music.SR:
            import soxr
            a = soxr.resample(a.T, music.SR, sr, quality="VHQ").T
        os.makedirs(MUSIC_DIR, exist_ok=True)
        sf.write(w + ".part.wav", np.clip(a.T, -1, 1), sr, subtype="PCM_24")
        os.replace(w + ".part.wav", w)
    return FileResponse(w, media_type="audio/wav")


@app.get("/api/music/peaks/{tid}")
def music_peaks(tid: str):
    import music
    t = _track(tid)
    if "_peaks" not in t:
        t["_peaks"] = music.peaks(t["path"], 50)
    return t["_peaks"]


def _stem_public(tid):
    t = tracks.get(tid) or {}
    j = _stem_jobs.get(tid, {"state": "none"})
    out = {k: j.get(k) for k in ("state", "progress", "message", "model", "error")}
    if t.get("stems"):
        out["stems"] = list(t["stems"].keys())
        out["state"] = "done"
        out["model"] = t.get("stems_model")
    out["track"] = _public(t) if t else None
    return out


def _run_stems(tid, model):
    import music
    t = tracks.get(tid)
    j = _stem_jobs[tid]
    try:
        def prog(f, msg):
            j.update(progress=round(f, 3), message=msg)
        files = music.split_stems(t["path"], STEMS_CACHE, os.path.join(engine.MODEL_DIR, "torch"), model, prog,
                                  lambda: j.get("cancel"))
        t["stems"], t["stems_model"] = files, model
        j.update(message="Checking the key with the stems…", progress=0.99)
        if not t.get("key_user"):
            try:
                t.update(music.key_from_stems(files, t["beats"]))
            except Exception as e:  # noqa
                print("stem key failed:", e)
        j.update(state="done", progress=1.0, message="Done")
    except InterruptedError:
        j.update(state="cancelled", message="Cancelled")
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        msg = str(e)
        if "No module named 'demucs'" in msg:
            msg = "The stem splitter isn't installed yet. Restart the app to finish its update."
        elif "urlopen" in msg or "HTTP Error" in msg or "Connection" in msg:
            msg = "Couldn't download the stem model. Check your internet connection and try again."
        j.update(state="error", error=msg, message=msg)


@app.post("/api/music/stems/{tid}")
def music_stems_start(tid: str, body: dict):
    global _stem_pool
    import music
    t = _track(tid)
    model = body.get("model") or load_settings().get("stem_model", "htdemucs")
    if model not in music.STEM_MODELS:
        model = "htdemucs"
    hit = music.cached_stems(STEMS_CACHE, t["path"], model)
    if hit:
        t["stems"], t["stems_model"] = hit, model
        if not t.get("key_user") and t.get("key_source") != "stems":
            try:
                t.update(music.key_from_stems(hit, t["beats"]))
            except Exception:
                pass
        _stem_jobs[tid] = {"state": "done", "progress": 1.0, "message": "Done", "model": model}
        return _stem_public(tid)
    j = _stem_jobs.get(tid)
    if j and j.get("state") in ("queued", "running"):
        return _stem_public(tid)
    t.pop("stems", None)
    if _stem_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _stem_pool = ThreadPoolExecutor(max_workers=1)      # one split at a time: they're heavy
    _stem_jobs[tid] = {"state": "running", "progress": 0.0, "message": "Waiting for the other split to finish…", "model": model}
    _stem_pool.submit(_run_stems, tid, model)
    return _stem_public(tid)


@app.get("/api/music/stems/{tid}")
def music_stems_status(tid: str):
    _track(tid)
    return _stem_public(tid)


@app.post("/api/music/stems/{tid}/cancel")
def music_stems_cancel(tid: str):
    j = _stem_jobs.get(tid)
    if j:
        j["cancel"] = True
    return {"ok": True}


@app.get("/api/music/stem/{tid}/{name}")
def music_stem_file(tid: str, name: str, wav: int = 0):
    t = _track(tid)
    p = (t.get("stems") or {}).get(name)
    if not p or not os.path.exists(p):
        raise HTTPException(404, "Stem not found")
    if wav:   # for web views that can't decode FLAC
        import music
        import hashlib
        key = hashlib.sha1(f"{p}|{os.path.getmtime(p)}".encode()).hexdigest()[:10]
        w = os.path.join(MUSIC_DIR, f"stem_{tid}_{name}_{key}.wav")
        if not os.path.exists(w):
            music.render(p, w, 0, None, 0, 1.0, fade=False)
        return FileResponse(w, media_type="audio/wav")
    return FileResponse(p, media_type="audio/flac")


@app.get("/api/music/stem_models")
def music_stem_models():
    import music
    return {"models": [{"id": k, **v} for k, v in music.STEM_MODELS.items()], "default": load_settings().get("stem_model", "htdemucs")}


@app.post("/api/music/save_deck")
def music_save_deck(body: dict):
    import music
    t = _track(body["id"])
    semis, ratio = float(body.get("semis", 0)), float(body.get("ratio", 1.0))
    gains = body.get("gains") or {}
    use_stems = t.get("stems") if gains else None
    kept = [n for n in (use_stems or {}) if float(gains.get(n, 1)) > 0.001]
    tag = []
    if semis:
        tag.append(f"{semis:+g} st")
    if abs(ratio - 1) > 1e-4:
        tag.append(f"{t['bpm'] * ratio:.1f} BPM")
    tag.append(music.shifted_key(t, int(round(semis)))["key"])
    if use_stems and len(kept) < len(use_stems):
        tag.append(" + ".join(kept) if kept else "silent")
    safe = "".join(ch for ch in t["name"] if ch not in '\\/:*?"<>|').strip() or "song"
    d = os.path.join(out_dir(), "Music")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"{safe} ({', '.join(tag)}).wav")
    music.render_mix(use_stems, t["path"], out, gains, semis, ratio)
    return {"output": out, "filename": os.path.basename(out)}


@app.post("/api/music/export_stems/{tid}")
def music_export_stems(tid: str):
    """Each stem as its own 24-bit WAV in <output>/Music/<song> stems/, ready for any editor."""
    import soundfile as sf
    import music
    t = _track(tid)
    stems = t.get("stems") or {}
    if not stems:
        raise HTTPException(400, "Split the stems first")
    safe = "".join(ch for ch in t["name"] if ch not in '\\/:*?"<>|').strip() or "song"
    d = os.path.join(out_dir(), "Music", f"{safe} stems")
    os.makedirs(d, exist_ok=True)
    for n, p in stems.items():
        sf.write(os.path.join(d, f"{safe} - {n}.wav"), music.audio(p).T, music.SR, subtype="PCM_24")
    return {"output": d, "count": len(stems)}


# ---- mix recorder: the window streams raw float32 stereo; saved as one 24-bit WAV in <output>/Music
_recs: dict[str, dict] = {}


@app.post("/api/music/rec/start")
def rec_start(body: dict):
    rid = uuid.uuid4().hex[:10]
    p = os.path.join(MUSIC_DIR, f"rec_{rid}.f32")
    open(p, "wb").close()
    _recs[rid] = {"path": p, "sr": int(body.get("sr") or 44100)}
    return {"id": rid}


@app.post("/api/music/rec/{rid}/chunk")
async def rec_chunk(rid: str, request: Request):
    r = _recs.get(rid)
    if not r:
        raise HTTPException(404, "No such recording")
    data = await request.body()
    with open(r["path"], "ab") as f:
        f.write(data[: len(data) // 8 * 8])
    return {"ok": True}


@app.post("/api/music/rec/{rid}/stop")
def rec_stop(rid: str):
    import datetime
    import soundfile as sf
    r = _recs.pop(rid, None)
    if not r:
        raise HTTPException(404, "No such recording")
    a = np.fromfile(r["path"], dtype=np.float32).reshape(-1, 2)
    os.remove(r["path"])
    loud = np.flatnonzero(np.abs(a).max(1) > 1e-4)
    if not len(loud):
        raise HTTPException(400, "Nothing was playing while recording")
    a = a[max(0, loud[0] - r["sr"] // 20): loud[-1] + r["sr"]]          # trim silence before the first sound
    d = os.path.join(out_dir(), "Music")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "Mix " + datetime.datetime.now().strftime("%Y-%m-%d %H.%M.%S") + ".wav")
    sf.write(out, np.clip(a, -1, 1), r["sr"], subtype="PCM_24")
    return {"output": out, "filename": os.path.basename(out), "seconds": len(a) / r["sr"]}


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/vendor/{name}")
def vendor_file(name: str):
    p = os.path.join(APP_DIR, "static", "vendor", os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="text/javascript" if p.endswith(("js", "mjs")) else None)


@app.delete("/api/music/{tid}")
def music_delete(tid: str):
    t = tracks.pop(tid, None)
    if t:
        try:
            os.remove(t["path"])
        except OSError:
            pass
    _clean_music_work((f"pcm_{tid}_", f"stem_{tid}_", f"full_{tid}"), older_than=-1)
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
def set_settings(body: dict):
    st = load_settings()
    for k, v in body.items():
        if k in DEFAULT_SETTINGS:
            st[k] = v
    if body.get("output_dir"):
        try:
            os.makedirs(st["output_dir"], exist_ok=True)
        except OSError:
            raise HTTPException(400, "That folder can't be used")
    save_settings(st)
    return st


@app.post("/api/settings/choose_folder")
def choose_folder():
    if not (NATIVE and WINDOW):
        raise HTTPException(400, "Folder picker is only available in the app window")
    import webview
    kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or getattr(webview, "FOLDER_DIALOG", 20)
    res = WINDOW.create_file_dialog(kind, directory=load_settings()["output_dir"])
    if not res:
        return {"cancelled": True}
    path = res[0] if isinstance(res, (list, tuple)) else res
    st = load_settings(); st["output_dir"] = path; save_settings(st)
    return st


@app.post("/api/open_folder")
def open_folder(body: dict):
    what = body.get("what")
    path = {"output": out_dir(), "data": SUPPORT, "log": LOG_FILE}.get(what)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Not available on this computer")
    _open(path, reveal=(what == "log"))
    return {"ok": True}


@app.get("/api/video/{vid}")
def video_file(vid: str):
    v = _video(vid)
    return FileResponse(v["path"])


# =====================================================================================
#  Voice (Voicebox) — see voice.py
# =====================================================================================
from fastapi import Request  # noqa: E402


def _vjob(kind, fn, *args, exclusive=True):
    try:
        j = voice.start_job(kind, fn, *args, exclusive=exclusive)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return j.as_dict()


def _vmedia(mid):
    try:
        return voice.get_media(mid)
    except KeyError as e:
        raise HTTPException(404, str(e))


def _vneed():
    try:
        voice.require()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


def _media_out(m):
    return {k: m[k] for k in ("id", "name", "info", "segments", "lang")} | {"target": m.get("target")}


@app.get("/voice", response_class=HTMLResponse)
def voice_page():
    with open(os.path.join(APP_DIR, "static", "voice.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/voice/status")
def voice_status():
    return voice.status()


@app.get("/api/voice/info")
def voice_info():
    return {"speak": voice.SPEAK_LANGS, "text": voice.TEXT_LANGS, "engines": voice.ENGINES,
            "ref": voice.VOICEBOX_REF[:8], "home": voice.HOME, "need_gb": voice.NEED_GB}


@app.post("/api/voice/setup")
def voice_setup():
    return voice.setup()


@app.post("/api/voice/setup/cancel")
def voice_setup_cancel():
    voice.cancel_setup()
    return voice.status()


@app.post("/api/voice/start")
def voice_start():
    return voice.start()


@app.post("/api/voice/stop")
def voice_stop():
    voice.stop()
    return voice.status()


@app.api_route("/api/voice/vb/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def voice_proxy(path: str, request: Request):
    """Pass-through to the Voicebox server (voices, presets, models)."""
    _vneed()
    body = await request.body()
    hdr = {"Content-Type": request.headers["content-type"]} if "content-type" in request.headers else {}
    q = ("?" + request.url.query) if request.url.query else ""
    import asyncio
    code, ctype, data = await asyncio.to_thread(voice.raw, request.method, "/" + path + q, body or None, hdr, 300)
    return Response(data, status_code=code, media_type=ctype or None)


@app.post("/api/voice/media/upload")
async def voice_upload(file: UploadFile = File(...)):
    d = os.path.join(voice.WORK, uuid.uuid4().hex[:10])
    os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(file.filename or "media.mp4")[1] or ".mp4"
    path = os.path.join(d, "input" + ext)
    with open(path, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            f.write(chunk)
    try:
        return _media_out(voice.add_media(path, file.filename or "media"))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, str(e))


@app.post("/api/voice/media/from_clean")
def voice_from_clean(body: dict):
    """Open the video that's loaded in Clean, or a finished cleaned video."""
    if body.get("job"):
        j = jobs.get(body["job"])
        if not j or j.state != "done":
            raise HTTPException(404, "That cleaned video isn't ready")
        path, name = j.out, os.path.basename(j.out)
    else:
        v = _video(body.get("vid", ""))
        path, name = v["path"], v["name"]
    try:
        return _media_out(voice.add_media(path, name, ref=body))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/voice/media/from_file")
def voice_from_file(body: dict):
    """Open a file Voice made (e.g. a finished dub) to keep working on it."""
    p = body.get("path", "")
    if not voice.allowed_file(p) or not os.path.exists(p):
        raise HTTPException(404, "File not found")
    try:
        return _media_out(voice.add_media(p, os.path.basename(p)))
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/voice/media/{mid}")
def voice_media(mid: str):
    return _media_out(_vmedia(mid))


@app.get("/api/voice/media/{mid}/file")
def voice_media_file(mid: str):
    return FileResponse(_vmedia(mid)["path"])


@app.post("/api/voice/media/{mid}/transcribe")
def voice_transcribe(mid: str, body: dict):
    _vneed(); _vmedia(mid)
    return _vjob("transcribe", voice.job_transcribe, mid, body.get("model", "turbo"), body.get("language") or "auto")


@app.post("/api/voice/media/{mid}/translate")
def voice_translate(mid: str, body: dict):
    _vneed(); _vmedia(mid)
    return _vjob("translate", voice.job_translate, mid, body.get("target", "en"), body.get("source"),
                 body.get("size", "4B"), body.get("segments"))


@app.post("/api/voice/media/{mid}/clone")
def voice_clone_media(mid: str, body: dict):
    _vneed(); _vmedia(mid)
    return _vjob("clone", voice.job_clone_from_media, mid, body.get("name"), body.get("segments"))


@app.post("/api/voice/media/{mid}/render")
def voice_render(mid: str, body: dict):
    _vneed(); _vmedia(mid)
    return _vjob("render", voice.job_render, mid, body)


@app.post("/api/voice/media/{mid}/tool")
def voice_tool(mid: str, body: dict):
    _vmedia(mid)
    return _vjob("tool", voice.job_audio_tool, mid, body.get("op"), body.get("opts") or {})


@app.post("/api/voice/media/{mid}/captions")
def voice_captions(mid: str, body: dict):
    _vmedia(mid)
    try:
        return voice.captions(mid, body.get("which", "original"), body.get("fmt", "srt"), body.get("segments"))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/voice/upload_audio")
async def voice_upload_audio(file: UploadFile = File(...)):
    d = os.path.join(voice.WORK, "uploads")
    os.makedirs(d, exist_ok=True)
    name = os.path.basename(file.filename or "audio.wav")
    path = os.path.join(d, uuid.uuid4().hex[:6] + "_" + name)
    with open(path, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            f.write(chunk)
    try:
        info = voice.probe(path)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"path": path, "name": name, "info": info}


@app.post("/api/voice/clone_file")
def voice_clone_file(body: dict):
    _vneed()
    p = body.get("path", "")
    if not voice.allowed_file(p):
        raise HTTPException(400, "Upload the sample first")
    return _vjob("clone", voice.job_clone_from_file, p, body.get("name"), body.get("language") or "en",
                 body.get("reference_text"))


@app.post("/api/voice/voiceover")
def voice_voiceover(body: dict):
    _vneed()
    if not (body.get("text") or "").strip() or not body.get("profile_id"):
        raise HTTPException(400, "Write a script and pick a voice")
    return _vjob("voiceover", voice.job_voiceover, body["profile_id"], body["text"].strip(),
                 body.get("language", "en"), body.get("engine"), body.get("model_size"), body.get("instruct"),
                 body.get("fmt", "wav"), body.get("title"))


@app.post("/api/voice/line")
def voice_line(body: dict):
    _vneed()
    if not (body.get("text") or "").strip() or not body.get("profile_id"):
        raise HTTPException(400, "Pick a voice first")
    return _vjob("line", voice.job_line, body["profile_id"], body["text"].strip(), body.get("language", "en"),
                 body.get("engine"), body.get("model_size"))


@app.get("/api/voice/jobs/{jid}")
def voice_job(jid: str):
    j = voice.jobs.get(jid)
    if not j:
        raise HTTPException(404, "Task not found")
    return j.as_dict()


@app.post("/api/voice/jobs/{jid}/cancel")
def voice_job_cancel(jid: str):
    j = voice.jobs.get(jid)
    if j:
        j.cancel()
    return {"ok": True}


@app.get("/api/voice/file")
def voice_file(p: str):
    if not voice.allowed_file(p) or not os.path.exists(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p)


@app.post("/api/voice/reveal")
def voice_reveal(body: dict):
    p = body.get("p", "")
    if body.get("what") in ("setup_log", "engine_log", "home"):
        p = {"setup_log": voice.SETUP_LOG, "engine_log": voice.ENGINE_LOG, "home": voice.HOME}[body["what"]]
        if not os.path.exists(p):
            raise HTTPException(404, "Not available yet")
        _open(p, reveal=body["what"] != "home")
        return {"ok": True}
    if not voice.allowed_file(p) or not os.path.exists(p):
        raise HTTPException(404, "File not found")
    _open(p, reveal=not body.get("open"))
    return {"ok": True}


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


def _win_identity():
    """Windows: own taskbar group instead of Python's."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TruStudio.CaptionRemover")
    except Exception:
        pass


def _win_icon():
    """Windows: put our icon on the window and its taskbar button."""
    try:
        import ctypes
        ico = os.environ.get("CR_ICON_ICO")
        if not ico or not os.path.exists(ico):
            return
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "Caption Remover")
        if not hwnd:
            return
        for size, which in ((256, 1), (32, 0)):          # ICON_BIG, ICON_SMALL
            h = u.LoadImageW(None, ico, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u.SendMessageW(hwnd, 0x80, which, h)          # WM_SETICON
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
            elif os.name == "nt":
                _win_identity()
            import webview  # pywebview
        except Exception as e:  # noqa
            print("Native window unavailable, using browser:", e)
            webview = None

    if webview is None:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        print(f"\n  Caption Remover running at {url}\n  Outputs save to: {out_dir()}\n  Close this window to stop.\n")
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
    global WINDOW
    WINDOW = webview.create_window("Caption Remover", url, width=1440, height=900, min_size=(1100, 700),
                          background_color="#0e0e10")
    if os.name == "nt":
        try:
            WINDOW.events.shown += _win_icon
        except Exception:
            pass
    if sys.platform == "darwin":
        _mac_icon()  # must run on the main thread, before the event loop starts
    webview.start(debug=bool(os.environ.get("CR_DEV")))  # dev: right-click > Reload / Inspect
    server.should_exit = True
    voice.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
