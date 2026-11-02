"""
Voice tools for Caption Remover, powered by Voicebox (github.com/jamiepine/voicebox, MIT).

Voicebox runs as a separate local server in its OWN Python environment, so its heavy and
version-pinned packages never touch the Caption Remover engine:

  <voice home>/
     voicebox/      Voicebox source at a pinned commit (VOICEBOX_REF)
     env/           its Python environment
     data/          voice profiles, generation history (SQLite)
     models/        downloaded voice models (Hugging Face cache)
     setup.log      install log      engine.log   server log

This module installs it on request (in-app, with progress), starts/stops the server, and
runs the video/audio jobs (transcribe, translate, clone, dub, fix lines, voiceover,
audio tools). Video and audio work uses the app's own ffmpeg.
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

import numpy as np

import engine

VOICEBOX_REF = "8af7efe62fab8d33e2a5dfbedabaa45f68a5184f"   # Voicebox commit we install (Oct 2026)
SRC_URL = f"https://codeload.github.com/jamiepine/voicebox/zip/{VOICEBOX_REF}"
SETUP_REV = "1"   # bump when the install recipe below changes
APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
NEED_GB = 15

if IS_MAC:
    APP_SUPPORT = os.environ.get("CR_SUPPORT") or os.path.expanduser("~/Library/Application Support/Caption Remover")
else:
    APP_SUPPORT = os.environ.get("CR_SUPPORT") or os.path.expanduser("~/.caption-remover")
HOME = os.environ.get("CR_VOICE_HOME") or os.path.join(APP_SUPPORT, "voice")
SRC = os.path.join(HOME, "voicebox")
ENV = os.path.join(HOME, "env")
DATA = os.path.join(HOME, "data")
MODELS = os.path.join(HOME, "models")
SETUP_LOG = os.path.join(HOME, "setup.log")
ENGINE_LOG = os.path.join(HOME, "engine.log")
STAMP = os.path.join(HOME, ".ready")
ENV_PY = os.path.join(ENV, "Scripts", "python.exe") if IS_WIN else os.path.join(ENV, "bin", "python3")

# Languages Voicebox can SPEAK (its API accepts these codes)
SPEAK_LANGS = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
    "tr": "Turkish", "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "el": "Greek", "he": "Hebrew", "ms": "Malay", "sw": "Swahili",
}
# Extra languages Whisper can TRANSCRIBE / the LLM can translate into (captions only)
TEXT_LANGS = dict(SPEAK_LANGS, yo="Yoruba", ha="Hausa", ig="Igbo", am="Amharic", zu="Zulu",
                  af="Afrikaans", id="Indonesian", vi="Vietnamese", th="Thai", uk="Ukrainian")
ENGINES = {
    "qwen": "Qwen3 TTS — best all-round, 10 languages",
    "chatterbox": "Chatterbox — expressive, multilingual",
    "chatterbox_turbo": "Chatterbox Turbo — fast, English",
    "omnivoice": "OmniVoice — widest language range",
    "luxtts": "LuxTTS — fast, light",
    "tada": "TADA — long-form English",
}
MODEL_PREFIX = {"qwen": "qwen-tts", "qwen_custom_voice": "qwen-custom-voice", "qwen_voice_design": "qwen-voice-design",
                "chatterbox": "chatterbox-tts", "chatterbox_turbo": "chatterbox-turbo", "luxtts": "luxtts",
                "tada": "tada", "kokoro": "kokoro", "omnivoice": "omnivoice"}

_FF = lambda: engine.FFMPEG
_FP = lambda: engine.FFPROBE
_NW = engine._NOWIN
SR = 48000

WORK = None            # set by init(): scratch folder for voice jobs
_out_dir = None        # set by init(): function returning the user's output folder


def init(work_dir, out_dir_fn):
    global WORK, _out_dir
    WORK = os.path.join(work_dir, "voice")
    os.makedirs(WORK, exist_ok=True)
    _out_dir = out_dir_fn


def out_dir():
    d = os.path.join(_out_dir(), "Voice")
    os.makedirs(d, exist_ok=True)
    return d


def _log(path, msg):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip("\n") + "\n")
    except OSError:
        pass


# =====================================================================================
#  Engine: install / start / stop / talk to Voicebox
# =====================================================================================
eng = {"state": "missing", "step": "", "progress": 0.0, "error": None, "port": None, "health": None}
_proc = None
_setup_proc = None
_lock = threading.Lock()


def installed():
    try:
        return open(STAMP).read().strip() == f"{VOICEBOX_REF}:{SETUP_REV}" and os.path.exists(ENV_PY)
    except OSError:
        return False


def status():
    if eng["state"] in ("missing", "installed") and installed():
        eng["state"] = "installed" if not _alive() else eng["state"]
    if eng["state"] == "ready" and not _alive():
        eng.update(state="stopped", error="The voice engine stopped. Check the engine log.")
    st = dict(eng)
    st.update(installed=installed(), home=HOME, supported=_supported())
    try:
        st["free_gb"] = round(shutil.disk_usage(APP_SUPPORT if os.path.exists(APP_SUPPORT) else os.path.expanduser("~")).free / 1e9, 1)
    except OSError:
        st["free_gb"] = None
    return st


def _supported():
    if IS_MAC:
        return platform.machine() == "arm64" or _sysctl_arm()
    return True


def _sysctl_arm():
    try:
        return subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True).stdout.strip() == "1"
    except Exception:
        return False


def _base_python():
    if IS_WIN:   # the app runs under pythonw.exe; venvs are made with python.exe next to it
        for d in (os.path.join(APP_SUPPORT, "python"), os.path.dirname(sys.executable)):
            if os.path.exists(os.path.join(d, "python.exe")):
                return os.path.join(d, "python.exe")
    cand = os.path.join(APP_SUPPORT, "python", "bin", "python3")
    if os.path.exists(cand):
        return cand
    return getattr(sys, "_base_executable", None) or sys.executable


def setup():
    """Install Voicebox in the background (idempotent: resumes after a failure)."""
    with _lock:
        if eng["state"] in ("installing", "starting"):
            return status()
        eng.update(state="installing", step="Preparing", progress=0.0, error=None)
    threading.Thread(target=_setup, daemon=True).start()
    return status()


def cancel_setup():
    eng["cancel"] = True
    if _setup_proc and _setup_proc.poll() is None:
        _setup_proc.kill()


def _run_logged(cmd, step, p0, p1, expect=60, cwd=None):
    """Run a command, log output, move progress from p0 toward p1 as lines arrive."""
    global _setup_proc
    eng.update(step=step)
    _log(SETUP_LOG, f"\n$ {' '.join(cmd)}")
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUNBUFFERED="1")
    env.pop("PYTHONPATH", None)
    _setup_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   cwd=cwd, env=env, **_NW)
    n = 0
    for line in _setup_proc.stdout:
        _log(SETUP_LOG, line)
        if line.startswith(("Collecting", "Downloading", "Building", "Installing", "Using cached")):
            n += 1
            eng["progress"] = p0 + (p1 - p0) * (1 - math.exp(-n / expect))
            m = re.match(r"(Collecting|Downloading)\s+([A-Za-z0-9_.\-\[\]]+)", line)
            if m:
                eng["step"] = f"{step} · {m.group(2)[:40]}"
    rc = _setup_proc.wait()
    if eng.get("cancel"):
        raise RuntimeError("Setup was cancelled")
    if rc != 0:
        raise RuntimeError(f"{step} failed (see setup log)")
    eng["progress"] = p1


def _setup():
    eng["cancel"] = False
    try:
        os.makedirs(HOME, exist_ok=True)
        _log(SETUP_LOG, f"\n===== {time.ctime()} setup Voicebox {VOICEBOX_REF[:8]} on {platform.platform()} =====")
        if not _supported():
            raise RuntimeError("The voice engine needs an Apple Silicon Mac (M1 or newer)")
        free = shutil.disk_usage(HOME).free / 1e9
        if free < NEED_GB and not installed():
            raise RuntimeError(f"Not enough disk space: {free:.0f} GB free, about {NEED_GB} GB needed")
        try:
            git_ok = subprocess.run(["git", "--version"], capture_output=True, timeout=20, **_NW).returncode == 0
        except Exception:
            git_ok = False
        if not git_ok:
            raise RuntimeError("Setup needs Apple's developer tools (for git). Open Terminal, run: xcode-select --install, then try again"
                               if IS_MAC else "Setup needs Git. Install it from git-scm.com, then try again")

        # 1) source
        if not os.path.exists(os.path.join(SRC, "backend", "requirements.txt")) or \
                _read(os.path.join(SRC, ".ref")) != VOICEBOX_REF:
            eng.update(step="Downloading Voicebox", progress=0.01)
            tmp = os.path.join(HOME, "voicebox.zip")
            req = urllib.request.Request(SRC_URL, headers={"User-Agent": "CaptionRemover"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            unpack = os.path.join(HOME, "src.new")
            shutil.rmtree(unpack, ignore_errors=True)
            with zipfile.ZipFile(tmp) as z:
                for n in z.namelist():
                    if n.startswith("/") or ".." in n.split("/"):
                        raise RuntimeError("Invalid Voicebox download")
                z.extractall(unpack)
            inner = os.path.join(unpack, os.listdir(unpack)[0])
            shutil.rmtree(SRC, ignore_errors=True)
            os.replace(inner, SRC)
            shutil.rmtree(unpack, ignore_errors=True)
            os.remove(tmp)
            open(os.path.join(SRC, ".ref"), "w").write(VOICEBOX_REF)
        eng["progress"] = 0.05

        # 2) its own Python environment
        if not os.path.exists(ENV_PY):
            _run_logged([_base_python(), "-m", "venv", ENV], "Creating the voice environment", 0.05, 0.08, 3)
        py = ENV_PY
        _run_logged([py, "-m", "pip", "install", "--upgrade", "pip", "-q"], "Updating installer", 0.08, 0.1, 2)

        # 3) packages (the same recipe Voicebox's own release build uses)
        req = os.path.join(SRC, "backend", "requirements.txt")
        _run_logged([py, "-m", "pip", "install", "-r", req], "Installing the voice engine", 0.1, 0.78, 160)
        _run_logged([py, "-m", "pip", "install", "--no-deps", "chatterbox-tts", "hume-tada", "omnivoice"],
                    "Installing extra voice models", 0.78, 0.84, 4)
        if IS_MAC:
            mlx = os.path.join(SRC, "backend", "requirements-mlx.txt")
            _run_logged([py, "-m", "pip", "install", "-r", mlx], "Installing Apple GPU support", 0.84, 0.92, 12)
            _run_logged([py, "-m", "pip", "install", "--no-deps", "mlx-lm==0.31.1", "mlx-audio==0.4.1"],
                        "Installing Apple GPU voice runtime", 0.92, 0.95, 3)
        # 4) check it imports
        _run_logged([py, "-c", "import fastapi, torch, transformers, librosa, soundfile; print('ok')"],
                    "Checking the install", 0.95, 0.99, 1)
        open(STAMP, "w").write(f"{VOICEBOX_REF}:{SETUP_REV}")
        eng.update(state="installed", step="Installed", progress=1.0)
        _log(SETUP_LOG, "===== setup complete =====")
        start()
    except Exception as e:  # noqa
        _log(SETUP_LOG, f"FAILED: {e}")
        eng.update(state="error", error=str(e))


def _read(p):
    try:
        return open(p).read().strip()
    except OSError:
        return ""


def _path_with_ffmpeg():
    """Put the app's own ffmpeg first on PATH for the voice engine (audio decoding)."""
    d = os.path.dirname(engine.FFMPEG) if os.path.isabs(engine.FFMPEG) else ""
    return os.pathsep.join(x for x in (d, os.environ.get("PATH", "")) if x)


def _alive():
    return _proc is not None and _proc.poll() is None


def _free_port(start=17600):
    for p in range(start, start + 60):
        with socket.socket() as so:
            try:
                so.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("No free port for the voice engine")


def start():
    """Start the Voicebox server (no-op if running)."""
    global _proc
    with _lock:
        if _alive() and eng["state"] in ("starting", "ready"):
            return status()
        if not installed():
            eng.update(state="missing")
            return status()
        port = int(os.environ.get("CR_VOICE_PORT") or _free_port())
        os.makedirs(DATA, exist_ok=True)
        os.makedirs(MODELS, exist_ok=True)
        env = dict(os.environ, VOICEBOX_MODELS_DIR=MODELS, HF_HUB_CACHE=MODELS, PYTORCH_ENABLE_MPS_FALLBACK="1",
                   PYTHONUNBUFFERED="1", TOKENIZERS_PARALLELISM="false", PATH=_path_with_ffmpeg())
        env.pop("PYTHONPATH", None)
        _log(ENGINE_LOG, f"\n===== {time.ctime()} start on port {port} =====")
        logf = open(ENGINE_LOG, "a", encoding="utf-8")
        _proc = subprocess.Popen([ENV_PY, os.path.join(APP_DIR, "voice_worker.py"), "serve", "--port", str(port),
                                  "--data-dir", DATA, "--parent", str(os.getpid())],
                                 cwd=SRC, env=env, stdout=logf, stderr=subprocess.STDOUT, **_NW)
        eng.update(state="starting", step="Starting the voice engine", port=port, error=None, health=None)
    threading.Thread(target=_wait_ready, daemon=True).start()
    return status()


def _wait_ready():
    t0 = time.time()
    while time.time() - t0 < 240:
        if not _alive():
            eng.update(state="error", error="The voice engine couldn't start. Check the engine log.")
            return
        try:
            eng["health"] = api("GET", "/health", timeout=3)
            eng.update(state="ready", step="Ready")
            return
        except Exception:
            time.sleep(1)
    eng.update(state="error", error="The voice engine took too long to start")


def stop():
    global _proc
    if _alive():
        try:
            _proc.terminate()
            _proc.wait(5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None
    if eng["state"] in ("ready", "starting"):
        eng["state"] = "installed"


def require():
    if eng["state"] != "ready" or not _alive():
        raise RuntimeError("The voice engine isn't running yet")


def _url(path):
    return f"http://127.0.0.1:{eng['port']}{path}"


def raw(method, path, body=None, headers=None, timeout=60):
    """Low-level call. Returns (status, content_type, bytes)."""
    req = urllib.request.Request(_url(path), data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def api(method, path, payload=None, timeout=60):
    body = json.dumps(payload).encode() if payload is not None else None
    code, ctype, data = raw(method, path, body, {"Content-Type": "application/json"} if body else {}, timeout)
    if code >= 400:
        try:
            detail = json.loads(data).get("detail")
        except Exception:
            detail = data[:300].decode(errors="replace")
        raise RuntimeError(detail if isinstance(detail, str) else json.dumps(detail))
    return json.loads(data) if data and "json" in ctype else data


def multipart(path, fields: dict, files: dict, timeout=120):
    b = "----cr" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, p in files.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{os.path.basename(p)}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + open(p, "rb").read() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    code, ctype, data = raw("POST", path, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout)
    if code >= 400:
        try:
            raise RuntimeError(json.loads(data).get("detail"))
        except (ValueError, AttributeError):
            raise RuntimeError(data[:300].decode(errors="replace"))
    return json.loads(data)


# =====================================================================================
#  Media helpers (ffmpeg)
# =====================================================================================
media: dict[str, dict] = {}


def probe(path):
    out = subprocess.run([_FP(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, **_NW).stdout
    j = json.loads(out or "{}")
    if not j.get("streams"):
        raise RuntimeError("That file doesn't look like audio or video ffmpeg can read")
    st = j["streams"]
    v = next((s for s in st if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in st if s.get("codec_type") == "audio"), None)
    dur = float((j.get("format") or {}).get("duration") or (v or a or {}).get("duration") or 0)
    info = {"duration": dur, "has_video": bool(v), "has_audio": bool(a)}
    if v:
        fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        n, d = (fr.split("/") + ["1"])[:2]
        info.update(width=v.get("width"), height=v.get("height"), vcodec=v.get("codec_name"),
                    fps=round(float(n) / float(d), 3) if float(d) else 0)
    if a:
        info.update(acodec=a.get("codec_name"), channels=a.get("channels"), sample_rate=int(a.get("sample_rate") or 0))
    return info


def add_media(path, name, ref=None):
    info = probe(path)
    if not info["has_audio"]:
        raise RuntimeError("This file has no sound to work with")
    mid = uuid.uuid4().hex[:10]
    media[mid] = {"id": mid, "path": path, "name": name, "info": info, "segments": [], "lang": None, "ref": ref}
    return media[mid]


def get_media(mid):
    m = media.get(mid)
    if not m:
        raise KeyError("File not found - please open it again")
    return m


def ff(args, timeout=None):
    r = subprocess.run([_FF(), "-v", "error", "-y"] + args, capture_output=True, timeout=timeout, **_NW)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace").strip()[-400:])
    return r.stdout


def decode(path, ch=2, start=None, end=None, filt=None):
    """Audio -> float32 numpy (n, ch) at 48 kHz."""
    a = []
    if start is not None:
        a += ["-ss", f"{start:.3f}"]
    if end is not None:
        a += ["-to", f"{end:.3f}"]
    a += ["-i", path, "-vn", "-ac", str(ch), "-ar", str(SR)]
    if filt:
        a += ["-af", filt]
    a += ["-f", "f32le", "-"]
    data = ff(a)
    return np.frombuffer(data, np.float32).reshape(-1, ch).copy()


def encode(audio, path, extra=None):
    """float32 (n, ch) at 48 kHz -> file (format from extension or extra args)."""
    ch = audio.shape[1]
    ext = os.path.splitext(path)[1].lower()
    codec = {".wav": ["-c:a", "pcm_s24le"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "256k"],
             ".m4a": ["-c:a", "aac", "-b:a", "256k"]}.get(ext, [])
    cmd = [_FF(), "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", str(ch), "-i", "pipe:0"] + (extra or codec) + [path]
    r = subprocess.run(cmd, input=np.ascontiguousarray(audio, np.float32).tobytes(), capture_output=True, **_NW)
    if r.returncode != 0 and ext == ".mp3":  # ffmpeg builds without lame
        return encode(audio, path[:-4] + ".m4a")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace")[-300:])
    return path


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)) + 1e-12)) if x.size else 0.0


def trim_silence(x, thr_db=-45):
    if not x.size:
        return x
    env = np.abs(x).max(axis=1)
    thr = 10 ** (thr_db / 20)
    idx = np.where(env > thr)[0]
    if not idx.size:
        return x[:0]
    pad = int(0.03 * SR)
    return x[max(0, idx[0] - pad): min(len(x), idx[-1] + pad)]


def fade(n, ms=30):
    k = max(1, min(n // 2, int(SR * ms / 1000)))
    return k


def unique(path):
    base, ext = os.path.splitext(path)
    k = 2
    while os.path.exists(path):
        path = f"{base}_{k}{ext}"
        k += 1
    return path


def slug(s, n=40):
    s = re.sub(r"[^\w\- ]+", "", s).strip().replace(" ", "_")
    return (s[:n] or "voice")


# =====================================================================================
#  Jobs
# =====================================================================================
jobs: dict[str, "Job"] = {}


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind, fn, *args, exclusive=True):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.state = "running"
        self.progress = 0.0
        self.step = "Starting"
        self.error = None
        self.result = None
        self.warnings = []
        self._cancel = False
        self.exclusive = exclusive
        self._t = threading.Thread(target=self._run, args=(fn, args), daemon=True)

    def _run(self, fn, args):
        try:
            self.result = fn(self, *args)
            self.state = "done"
            self.progress = 1.0
        except Cancelled:
            self.state = "cancelled"
        except Exception as e:  # noqa
            self.state = "error"
            self.error = str(e)
            _log(os.path.join(HOME, "jobs.log"), f"{time.ctime()} {self.kind} failed: {e!r}")

    def tick(self, p=None, step=None):
        if self._cancel:
            raise Cancelled()
        if p is not None:
            self.progress = max(0.0, min(1.0, p))
        if step:
            self.step = step

    def cancel(self):
        self._cancel = True

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "state": self.state, "progress": round(self.progress, 3),
                "step": self.step, "error": self.error, "result": self.result, "warnings": self.warnings}


def start_job(kind, fn, *args, exclusive=True):
    if exclusive:
        for j in jobs.values():
            if j.exclusive and j.state == "running":
                raise RuntimeError("Another voice task is still running. Wait for it or cancel it first.")
    j = Job(kind, fn, *args, exclusive=exclusive)
    jobs[j.id] = j
    j._t.start()
    return j


# ---------- speech generation (Voicebox) ----------
def _ensure_model_note(job, eng_name, size):
    """If the voice model isn't downloaded yet, say so (generation downloads it)."""
    try:
        st = api("GET", "/models/status", timeout=15)
        pre = MODEL_PREFIX.get(eng_name or "qwen", "")
        for m in st.get("models", []):
            if m["model_name"].startswith(pre) and (not size or size in m["model_name"] or pre in ("luxtts", "chatterbox-tts", "chatterbox-turbo", "kokoro", "omnivoice")):
                if not m.get("downloaded"):
                    mb = m.get("size_mb")
                    job.tick(step=f"Downloading {m['display_name']} (first time only{f', ~{mb/1000:.1f} GB' if mb else ''})")
                return
    except Exception:
        pass


def generate(job, profile_id, text, language="en", engine_name=None, model_size=None, instruct=None, dest=None):
    """Speak `text` with a Voicebox profile; returns path of a WAV."""
    if language not in SPEAK_LANGS:
        raise RuntimeError(f"The voice engine can't speak {TEXT_LANGS.get(language, language)} yet")
    body = {"profile_id": profile_id, "text": text, "language": language, "normalize": True}
    if engine_name:
        body["engine"] = engine_name
    if model_size:
        body["model_size"] = model_size
    if instruct:
        body["instruct"] = instruct[:500]
    g = api("POST", "/generate", body, timeout=60)
    gid = g["id"]
    t0 = time.time()
    while True:
        if job._cancel:
            try:
                api("POST", f"/generate/{gid}/cancel", {}, timeout=10)
            except Exception:
                pass
            raise Cancelled()
        h = api("GET", f"/history/{gid}", timeout=20)
        if h.get("status") == "completed":
            break
        if h.get("status") in ("failed", "cancelled"):
            raise RuntimeError(h.get("error") or "Speech generation failed")
        if time.time() - t0 > 3600:
            raise RuntimeError("Speech generation timed out")
        time.sleep(0.6)
    data = api("GET", f"/audio/{gid}", timeout=120)
    dest = dest or os.path.join(WORK, f"gen_{gid}.wav")
    with open(dest, "wb") as f:
        f.write(data)
    return dest


# ---------- transcription (Whisper, with timestamps, in the voice env) ----------
def _transcribe_file(job, wav, model="turbo", language=None, duration=None, p0=0.0, p1=1.0):
    cmd = [ENV_PY, os.path.join(APP_DIR, "voice_worker.py"), "transcribe", wav, "--model", model]
    if language and language != "auto":
        cmd += ["--language", language]
    if duration:
        cmd += ["--duration", str(duration)]
    env = dict(os.environ, HF_HUB_CACHE=MODELS, PYTORCH_ENABLE_MPS_FALLBACK="1", TOKENIZERS_PARALLELISM="false",
               PATH=_path_with_ffmpeg())
    env.pop("PYTHONPATH", None)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=WORK, **_NW)
    tail = []

    def read_err():
        for line in p.stderr:
            tail.append(line)
            del tail[:-30]
            m = re.match(r"PROGRESS ([\d.]+) (.*)", line.strip())
            if m:
                job.progress = p0 + (p1 - p0) * float(m.group(1))
                job.step = m.group(2)
    th = threading.Thread(target=read_err, daemon=True)
    th.start()
    while p.poll() is None:
        if job._cancel:
            p.kill()
            raise Cancelled()
        time.sleep(0.3)
    th.join(2)
    out = p.stdout.read()
    _log(os.path.join(HOME, "jobs.log"), "".join(tail))
    if p.returncode != 0:
        raise RuntimeError("Transcription failed: " + "".join(tail)[-300:].strip())
    last = [l for l in out.strip().splitlines() if l.startswith("{")]
    return json.loads(last[-1])["segments"]


def _tidy(segs):
    out = []
    for s in segs:
        if out and (s["end"] - s["start"] < 0.6 or len(s["text"]) < 4) and s["start"] - out[-1]["end"] < 0.4:
            out[-1]["end"] = s["end"]
            out[-1]["text"] = (out[-1]["text"] + " " + s["text"]).strip()
        else:
            out.append(dict(s))
    for i, s in enumerate(out):
        s["i"] = i
    return out


def job_transcribe(job, mid, model, language):
    m = get_media(mid)
    job.tick(0.02, "Preparing the audio")
    wav = os.path.join(WORK, f"{mid}_16k.wav")
    ff(["-i", m["path"], "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
    segs = _tidy(_transcribe_file(job, wav, model, language, m["info"]["duration"], 0.05, 1.0))
    d = m["info"]["duration"]
    for s in segs:
        s["end"] = round(min(s["end"], d), 3) if d else s["end"]
        s["start"] = round(min(s["start"], max(0.0, s["end"] - 0.2)), 3)
    m["segments"] = segs
    m["lang"] = None if language in (None, "", "auto") else language
    return {"segments": segs}


# ---------- translation (Voicebox's local LLM) ----------
def _strip_think(t):
    return re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()


def job_translate(job, mid, target, source, size, segments):
    m = get_media(mid)
    if segments:
        m["segments"] = segments
    segs = m["segments"]
    if not segs:
        raise RuntimeError("Transcribe the file first")
    tname = TEXT_LANGS.get(target, target)
    sname = TEXT_LANGS.get(source, "the original language") if source and source != "auto" else "the original language"
    system = (f"You translate video subtitles from {sname} into {tname}. Translate every numbered line. "
              f"Reply with the same numbers, one line each, in the form 'N. translation'. Keep each line natural "
              f"and about as long as the original so it fits the same timing. Output only the numbered lines.")
    B = 8
    for b in range(0, len(segs), B):
        job.tick(b / len(segs) * 0.98, f"Translating to {tname} ({b + 1}–{min(b + B, len(segs))} of {len(segs)})")
        chunk = segs[b:b + B]
        prompt = "\n".join(f"{k + 1}. {s['text']}" for k, s in enumerate(chunk))
        r = api("POST", "/llm/generate", {"prompt": prompt, "system": system, "model_size": size,
                                          "max_tokens": min(4096, 120 + 90 * len(chunk)), "temperature": 0.2},
                timeout=1800)
        got = {}
        for line in _strip_think(r.get("text", "")).splitlines():
            mm = re.match(r"\s*\[?(\d+)[.)\]:]\s*(.+)", line)
            if mm:
                got[int(mm.group(1))] = mm.group(2).strip()
        for k, s in enumerate(chunk):
            if k + 1 in got:
                s["tr"] = got[k + 1]
            else:  # model skipped a line: translate it alone
                r1 = api("POST", "/llm/generate", {"prompt": f"1. {s['text']}", "system": system, "model_size": size,
                                                   "max_tokens": 300, "temperature": 0.2}, timeout=600)
                t = _strip_think(r1.get("text", ""))
                s["tr"] = re.sub(r"^\s*\[?1[.)\]:]\s*", "", t.splitlines()[0] if t else "").strip() or s["text"]
    m["target"] = target
    return {"segments": segs}


# ---------- cloning ----------
def _pick_reference(segs, dur):
    good = [s for s in segs if 3.0 <= s["end"] - s["start"] <= 25 and len(s["text"]) >= 12]
    good.sort(key=lambda s: -(s["end"] - s["start"]))
    pick, tot = [], 0.0
    for s in good:
        if len(pick) == 3 or tot > 40:
            break
        pick.append(s)
        tot += s["end"] - s["start"]
    if pick:
        return [(s["start"], s["end"], s["text"]) for s in pick]
    # join neighbouring short lines into one 4-25 s window
    for i in range(len(segs)):
        j, text = i, []
        while j < len(segs) and segs[j]["end"] - segs[i]["start"] < 25:
            text.append(segs[j]["text"])
            if segs[j]["end"] - segs[i]["start"] >= 4:
                return [(segs[i]["start"], segs[j]["end"], " ".join(text))]
            j += 1
    raise RuntimeError("Couldn't find at least 3 seconds of clear speech to learn the voice from")


def _make_profile(job, name, language, refs, src_path):
    lang = language if language in SPEAK_LANGS else "en"
    job.tick(0.7, "Creating the voice")
    prof = api("POST", "/profiles", {"name": name[:100], "language": lang, "voice_type": "cloned",
                                     "description": "Made in Caption Remover"})
    try:
        for k, (s, e, text) in enumerate(refs):
            job.tick(0.75 + 0.2 * k / len(refs), f"Teaching it the voice ({k + 1} of {len(refs)})")
            wav = os.path.join(WORK, f"ref_{prof['id']}_{k}.wav")
            ff(["-ss", f"{max(0, s - 0.05):.3f}", "-to", f"{e + 0.1:.3f}", "-i", src_path, "-vn", "-ac", "1",
                "-ar", "24000", "-af", "highpass=f=70,loudnorm=I=-20:TP=-2", wav])
            multipart(f"/profiles/{prof['id']}/samples", {"reference_text": text}, {"file": wav})
    except Exception:
        try:
            api("DELETE", f"/profiles/{prof['id']}")
        except Exception:
            pass
        raise
    return prof


def job_clone_from_media(job, mid, name, segments):
    m = get_media(mid)
    if segments:
        m["segments"] = segments
    if not m["segments"]:
        raise RuntimeError("Transcribe the file first, so the voice can be matched to its words")
    refs = _pick_reference(m["segments"], m["info"]["duration"])
    prof = _make_profile(job, name or f"{os.path.splitext(m['name'])[0]} voice", m.get("lang") or "en", refs, m["path"])
    return {"profile": prof}


def job_clone_from_file(job, path, name, language, reference_text):
    info = probe(path)
    dur = min(info["duration"], 28.0)
    if dur < 2.5:
        raise RuntimeError("The sample is too short. Use 5 to 25 seconds of clear speech")
    text = (reference_text or "").strip()
    if not text:
        job.tick(0.05, "Listening to the sample")
        wav = os.path.join(WORK, f"cl_{job.id}.wav")
        ff(["-i", path, "-t", f"{dur:.2f}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])
        segs = _transcribe_file(job, wav, "turbo", language if language != "auto" else None, dur, 0.05, 0.65)
        text = " ".join(s["text"] for s in segs).strip()
        if not text:
            raise RuntimeError("No speech found in that sample")
    return {"profile": _make_profile(job, name or os.path.splitext(os.path.basename(path))[0], language, [(0, dur - 0.1, text)], path)}


# ---------- voiceover from a script ----------
def job_voiceover(job, profile_id, text, language, engine_name, model_size, instruct, fmt, title):
    job.tick(0.03, "Preparing")
    _ensure_model_note(job, engine_name, model_size)
    if not job.step.startswith("Downloading"):
        job.tick(0.08, "Speaking your script")
    tmp = generate(job, profile_id, text, language, engine_name, model_size, instruct)
    job.tick(0.92, "Saving")
    a = decode(tmp, 1)
    name = slug(title or text.split("\n")[0], 36)
    out = unique(os.path.join(out_dir(), f"{name}.{fmt if fmt in ('wav', 'mp3') else 'wav'}"))
    out = encode(np.repeat(a, 2, axis=1), out)
    return {"path": out, "file": os.path.basename(out), "duration": len(a) / SR, "preview": tmp}


def job_line(job, profile_id, text, language, engine_name, model_size):
    _ensure_model_note(job, engine_name, model_size)
    if not job.step.startswith("Downloading"):
        job.tick(0.1, "Speaking the line")
    tmp = generate(job, profile_id, text, language, engine_name, model_size)
    return {"preview": tmp, "duration": len(decode(tmp, 1)) / SR}


# ---------- dub / fix lines ----------
def _fit(path, slot, max_rate=1.35):
    """Decode a generated line, trim silence, speed up (pitch-safe) if it overruns the slot."""
    x = trim_silence(decode(path, 1))
    dur = len(x) / SR
    rate = 1.0
    if slot > 0.3 and dur > slot * 1.02:
        rate = min(max_rate, dur / slot)
        x = trim_silence(decode(path, 1, filt=f"atempo={rate:.4f}"))
    return x, rate


def job_render(job, mid, opts):
    m = get_media(mid)
    info = m["info"]
    segs = opts.get("segments") or m["segments"]
    if opts.get("segments"):   # keep already-generated lines so re-renders only voice what changed
        prev = {(round(s.get("start", 0), 2), s.get("_gen_text")): s for s in m["segments"] if s.get("_gen")}
        for s in segs:
            for k, v in (prev.get((round(s.get("start", 0), 2), s.get("_gen_text") or (s.get("tr") or s.get("text") or "").strip())) or {}).items():
                if k.startswith("_gen"):
                    s.setdefault(k, v)
        m["segments"] = segs
    mode = opts.get("mode", "dub")                      # dub | fix
    lang = opts.get("language") or "en"
    under = float(opts.get("under", 0.1 if mode == "dub" else 0.0))
    prof = opts.get("profile_id")
    if not prof:
        raise RuntimeError("Pick a voice first")
    use_tr = mode == "dub" and opts.get("use_translation", True)
    todo = []
    for k, s in enumerate(segs):
        text = ((s.get("tr") if use_tr else None) or s.get("text") or "").strip()
        if mode == "fix" and not s.get("replace"):
            continue
        if not text:
            continue
        nxt = segs[k + 1]["start"] if k + 1 < len(segs) else info["duration"]
        slot = max(0.3, min(nxt, s["end"] + (0.8 if mode == "dub" else 0.35)) - s["start"])
        todo.append((s, text, slot))
    if not todo:
        raise RuntimeError("Nothing to voice. Edit a line and tick it, or switch to Dub" if mode == "fix" else
                           "There are no lines to voice")

    job.tick(0.02, "Reading the original sound")
    orig = decode(m["path"], 2)
    total = int(round(info["duration"] * SR)) or len(orig)
    if len(orig) < total:
        orig = np.vstack([orig, np.zeros((total - len(orig), 2), np.float32)])
    orig = orig[:total]
    voice = np.zeros((total, 1), np.float32)
    gain = np.ones(total, np.float32)
    if mode == "dub":
        gain[:] = under
    _ensure_model_note(job, opts.get("engine"), opts.get("model_size"))

    speech_rms = []
    for s, _, _ in todo:
        a, b = int(s["start"] * SR), int(s["end"] * SR)
        r = rms(orig[a:b])
        if r > 1e-4:
            speech_rms.append(r)
    ref_level = float(np.median(speech_rms)) if speech_rms else 0.1

    long_lines = 0
    for n, (s, text, slot) in enumerate(todo):
        if not job.step.startswith("Downloading") or n:
            job.tick(0.05 + 0.85 * n / len(todo), f"Voicing line {n + 1} of {len(todo)}")
        cache = s.get("_gen") if s.get("_gen_text") == text and s.get("_gen_prof") == prof and os.path.exists(s.get("_gen") or "") else None
        wav = cache or generate(job, prof, text, lang, opts.get("engine"), opts.get("model_size"), opts.get("instruct"))
        s["_gen"], s["_gen_text"], s["_gen_prof"] = wav, text, prof
        x, rate = _fit(wav, slot)
        if len(x) / SR > slot + 0.25:
            long_lines += 1
        a = int(s["start"] * SR)
        b = min(total, a + len(x))
        if b <= a:
            continue
        # level-match: the original line's loudness (fix) or typical speech level (dub)
        target = rms(orig[a:int(s["end"] * SR)]) if mode == "fix" else ref_level
        g = (target if target > 1e-3 else ref_level) / max(rms(x), 1e-4)
        x = x[: b - a] * min(g, 8.0)
        voice[a:b] += x
        if mode == "fix":
            e = max(b, int(s["end"] * SR))
            k = fade(e - a)
            env = np.full(e - a, under, np.float32)
            env[:k] = np.linspace(1, under, k)
            env[-k:] = np.linspace(under, 1, k)
            gain[a:e] = np.minimum(gain[a:e], env)
    if long_lines:
        job.warnings.append(f"{long_lines} line(s) were longer than their slot even after speeding up a little. "
                            f"Shorten those lines and render again for tighter timing.")

    job.tick(0.92, "Mixing")
    mix = orig * gain[:, None] + voice
    peak = float(np.abs(mix).max()) if mix.size else 0
    if peak > 0.98:
        mix *= 0.98 / peak
    base = os.path.splitext(os.path.basename(m["name"]))[0]
    tag = f"dub_{lang}" if mode == "dub" else "fixed"
    od = out_dir()
    stem = unique(os.path.join(od, f"{base}_{tag}_voice.wav"))
    vv = voice * (0.98 / max(0.98, float(np.abs(voice).max()) if voice.size else 1))
    encode(np.repeat(vv, 2, axis=1), stem)

    job.tick(0.96, "Saving")
    if info["has_video"]:
        ext = ".mov" if (os.path.splitext(m["path"])[1].lower() in (".mov", ".mxf") or
                         (info.get("vcodec") or "") in ("prores", "dnxhd")) else ".mp4"
        out = _mux(m["path"], mix, os.path.join(od, f"{base}_{tag}{ext}"))
    else:
        out = encode(mix, unique(os.path.join(od, f"{base}_{tag}.wav")))
    return {"path": out, "file": os.path.basename(out), "stem": stem, "lines": len(todo)}


def _mux(video, mix, out):
    """Put a new soundtrack on a video without re-encoding the picture."""
    tmp = os.path.join(WORK, f"mix_{uuid.uuid4().hex[:6]}.wav")
    encode(mix, tmp, ["-c:a", "pcm_f32le"])
    tries = [out] + ([out[:-4] + ".mov"] if out.endswith(".mp4") else [])
    last = None
    try:
        for t in tries:
            t = unique(t)
            mp4 = t.endswith(".mp4")
            ac = ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"] if mp4 else ["-c:a", "pcm_s24le"]
            try:
                ff(["-i", video, "-i", tmp, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"] + ac + [t])
                return t
            except RuntimeError as e:
                last = e
                if os.path.exists(t):
                    os.remove(t)
        raise RuntimeError(f"Couldn't save the video ({last})")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------- audio tools (no voice engine needed) ----------
def job_audio_tool(job, mid, op, opts):
    m = get_media(mid)
    info = m["info"]
    base = os.path.splitext(os.path.basename(m["name"]))[0]
    od = out_dir()
    job.tick(0.1, {"extract": "Extracting the audio", "normalize": "Balancing loudness",
                   "denoise": "Cleaning up noise", "replace": "Replacing the audio",
                   "mix": "Adding the audio"}.get(op, "Working"))
    if op == "extract":
        fmt = opts.get("fmt", "wav")
        out = unique(os.path.join(od, f"{base}_audio.{fmt}"))
        ff(["-i", m["path"], "-vn"] + (["-c:a", "pcm_s24le"] if fmt == "wav" else ["-c:a", "libmp3lame", "-b:a", "320k"]) + [out])
        return {"path": out, "file": os.path.basename(out)}

    if op in ("normalize", "denoise"):
        lufs = float(opts.get("lufs", -14))
        if op == "normalize":
            filt = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
            tag = f"{int(abs(lufs))}LUFS"
        else:
            strength = {"light": 8, "medium": 14, "strong": 22}.get(opts.get("strength", "medium"), 14)
            filt = f"highpass=f=70,afftdn=nr={strength}:nf=-40:tn=1,lowpass=f=16000"
            tag = "clean"
        a = decode(m["path"], 2, filt=filt)
    elif op in ("replace", "mix"):
        src = opts.get("audio_path")
        if not src or not os.path.exists(src):
            raise RuntimeError("Choose the audio file to use")
        new = decode(src, 2)
        total = int(round(info["duration"] * SR))
        if op == "replace":
            a = np.zeros((total, 2), np.float32)
            a[:min(total, len(new))] = new[:total]
            tag = "newaudio"
        else:
            orig = decode(m["path"], 2)
            orig = np.vstack([orig, np.zeros((max(0, total - len(orig)), 2), np.float32)])[:total]
            at = int(float(opts.get("at", 0)) * SR)
            under = float(opts.get("under", 0.35))
            b = min(total, at + len(new))
            gain = np.ones(total, np.float32)
            if b > at:
                k = fade(b - at, 250)
                env = np.full(b - at, under, np.float32)
                env[:k] = np.linspace(1, under, k)
                env[-k:] = np.linspace(under, 1, k)
                gain[at:b] = env
                orig = orig * gain[:, None]
                orig[at:b] += new[: b - at] * float(opts.get("level", 1.0))
            a = orig
            pk = float(np.abs(a).max()) if a.size else 0
            if pk > 0.98:
                a *= 0.98 / pk
            tag = "voiceover"
    else:
        raise RuntimeError("Unknown tool")
    job.tick(0.8, "Saving")
    if info["has_video"]:
        ext = ".mov" if (os.path.splitext(m["path"])[1].lower() in (".mov", ".mxf") or (info.get("vcodec") or "") in ("prores", "dnxhd")) else ".mp4"
        out = _mux(m["path"], a, os.path.join(od, f"{base}_{tag}{ext}"))
    else:
        out = encode(a, unique(os.path.join(od, f"{base}_{tag}.wav")))
    return {"path": out, "file": os.path.basename(out)}


# ---------- captions ----------
def _ts(t, sep=","):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    mnt, s = divmod(r, 60)
    return f"{int(h):02d}:{int(mnt):02d}:{int(s):02d}{sep}{int(round((s - int(s)) * 1000)) % 1000:03d}"


def captions(mid, which, fmt, segments=None):
    m = get_media(mid)
    if segments:
        m["segments"] = segments
    segs = m["segments"]
    if not segs:
        raise RuntimeError("Transcribe the file first")
    key = "tr" if which == "translation" else "text"
    rows = [(s["start"], s["end"], (s.get(key) or "").strip()) for s in segs if (s.get(key) or "").strip()]
    if not rows:
        raise RuntimeError("There's no translation yet" if key == "tr" else "No text to export")
    if fmt == "srt":
        body = "\n".join(f"{i + 1}\n{_ts(a)} --> {_ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(rows))
    elif fmt == "vtt":
        body = "WEBVTT\n\n" + "\n".join(f"{_ts(a, '.')} --> {_ts(b, '.')}\n{t}\n" for a, b, t in rows)
    else:
        body = "\n".join(t for _, _, t in rows) + "\n"
    base = os.path.splitext(os.path.basename(m["name"]))[0]
    suf = f"_{m.get('target')}" if key == "tr" and m.get("target") else ""
    out = unique(os.path.join(out_dir(), f"{base}{suf}.{fmt}"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    return {"path": out, "file": os.path.basename(out)}


def allowed_file(p):
    rp = os.path.realpath(p)
    roots = [WORK, out_dir(), _out_dir(), DATA]
    return any(rp.startswith(os.path.realpath(r) + os.sep) for r in roots if r)
