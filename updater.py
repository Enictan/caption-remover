"""
In-app updates.

Feed: a small JSON file published with every release, e.g.
  https://github.com/<user>/caption-remover-updates/releases/latest/download/latest.json
  {"version": "1.0.2", "url": "<zip url>", "sha256": "...", "size": 123, "notes": "..."}

The zip holds only the app code (app.py, engine.py, updater.py, static/, requirements.txt).
It's unpacked into <support>/app. The launcher runs whichever is newer: that folder or the
code bundled inside the .app. If the new code needs new building blocks, the launcher
installs them on the next start (requirements.txt changed).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.environ.get("CR_SUPPORT")          # set by the packaged app's launcher
BUNDLE = os.environ.get("CR_BUNDLE")            # path to Caption Remover.app (Mac)
UA = {"User-Agent": "CaptionRemover-Updater"}

state = {"state": "idle", "progress": 0.0, "error": None, "version": None, "needs_setup": False}
_lock = threading.Lock()


def vtuple(v: str):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def feed_url():
    if os.environ.get("CR_UPDATE_FEED"):
        return os.environ["CR_UPDATE_FEED"]
    try:
        with open(os.path.join(APP_DIR, "update.json"), encoding="utf-8") as f:
            return json.load(f).get("feed")
    except Exception:
        return None


def enabled():
    return bool(SUPPORT and feed_url() and not os.environ.get("CR_DEV"))


def _broken_version():
    try:
        with open(os.path.join(SUPPORT, "app.broken", "app.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("VERSION = "):
                    return line.split('"')[1]
    except Exception:
        pass
    return None


def check(current: str) -> dict:
    if not enabled():
        return {"enabled": False, "available": False, "current": current}
    req = urllib.request.Request(feed_url(), headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        feed = json.loads(r.read().decode())
    latest = str(feed.get("version", "0"))
    available = vtuple(latest) > vtuple(current) and latest != _broken_version()
    return {"enabled": True, "available": available, "current": current, "version": latest,
            "notes": feed.get("notes", ""), "size": feed.get("size")}


def install(current: str):
    """Start download+install in the background. Poll `state`."""
    with _lock:
        if state["state"] in ("downloading", "installing"):
            return
        state.update(state="downloading", progress=0.0, error=None, version=None, needs_setup=False)
    threading.Thread(target=_install, args=(current,), daemon=True).start()


def _install(current):
    tmp_zip = os.path.join(SUPPORT, "update.zip.part")
    new_dir = os.path.join(SUPPORT, "app.new")
    try:
        req = urllib.request.Request(feed_url(), headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            feed = json.loads(r.read().decode())
        if vtuple(feed["version"]) <= vtuple(current):
            raise RuntimeError("Already up to date")
        state["version"] = feed["version"]

        # download with progress + checksum
        h = hashlib.sha256()
        req = urllib.request.Request(feed["url"], headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp_zip, "wb") as f:
            total = int(r.headers.get("Content-Length") or feed.get("size") or 0)
            got = 0
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    state["progress"] = min(0.95, got / total * 0.95)
        if feed.get("sha256") and h.hexdigest() != feed["sha256"]:
            raise RuntimeError("Download was corrupted (checksum mismatch). Please try again.")

        # unpack + sanity check
        state["state"] = "installing"
        shutil.rmtree(new_dir, ignore_errors=True)
        with zipfile.ZipFile(tmp_zip) as z:
            for n in z.namelist():  # refuse paths that escape the folder
                if n.startswith("/") or ".." in n.split("/"):
                    raise RuntimeError("Invalid update package")
            z.extractall(new_dir)
        if not os.path.exists(os.path.join(new_dir, "app.py")):
            raise RuntimeError("Invalid update package (app.py missing)")

        def req_txt(d):
            try:
                with open(os.path.join(d, "requirements.txt"), encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""
        state["needs_setup"] = req_txt(new_dir) != req_txt(APP_DIR)

        # swap in
        cur_dir = os.path.join(SUPPORT, "app")
        old_dir = os.path.join(SUPPORT, "app.old")
        shutil.rmtree(old_dir, ignore_errors=True)
        if os.path.exists(cur_dir):
            os.replace(cur_dir, old_dir)
        os.replace(new_dir, cur_dir)
        shutil.rmtree(old_dir, ignore_errors=True)
        shutil.rmtree(os.path.join(SUPPORT, "app.broken"), ignore_errors=True)
        state.update(state="ready", progress=1.0)
    except Exception as e:  # noqa
        state.update(state="error", error=str(e))
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass
        shutil.rmtree(new_dir, ignore_errors=True)


def restart():
    """Relaunch the app so the launcher picks up the new code, then quit this process."""
    if sys.platform == "darwin" and BUNDLE and BUNDLE.endswith(".app"):
        subprocess.Popen(["/bin/sh", "-c", f'sleep 1.5; open -n "{BUNDLE}"'], start_new_session=True)
    elif os.name == "nt" and BUNDLE:
        subprocess.Popen([BUNDLE], creationflags=0x00000008)  # DETACHED_PROCESS
    os._exit(0)
