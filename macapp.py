"""
Make macOS show "Caption Remover" (name + icon) in the Dock, menu bar, ⌘-Tab and
Activity Monitor instead of "python3.12".

macOS names a running process after the app bundle its executable lives in. Our Python
lives in ~/Library/Application Support/Caption Remover/python, outside any bundle, so we
build a tiny runtime bundle once:

  <support>/runtime/Caption Remover.app/Contents/
      Info.plist            name, icon, bundle id
      MacOS/Caption Remover a copy of the Python interpreter
      lib -> <support>/python/lib   (so the interpreter finds its libraries)
      Resources/AppIcon.icns

...and re-start ourselves from inside it (same PID, via exec). If anything goes wrong we
simply keep running as plain Python, so this can never stop the app from opening.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys

RUNTIME_REV = "1"   # bump to force a rebuild of the runtime bundle


def _log(msg):
    print(f"[macapp] {msg}", file=sys.stderr, flush=True)


def ensure_bundle():
    if sys.platform != "darwin" or os.environ.get("CR_IN_BUNDLE") or os.environ.get("CR_NO_BUNDLE"):
        return
    support = os.environ.get("CR_SUPPORT") or os.path.expanduser("~/Library/Application Support/Caption Remover")
    if ".app/Contents/MacOS/" in sys.executable:
        return
    try:
        exe = _build(support)
        env = dict(os.environ, CR_IN_BUNDLE="1", PYTHONHOME=sys.base_prefix)
        # quick self-test before switching, so a bad copy can't break startup
        r = subprocess.run([exe, "-c", "import encodings, ssl, json"], env=env, capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore")[-300:])
        _log(f"relaunching as bundle: {exe}")
        os.execve(exe, [exe] + sys.argv, env)
    except Exception as e:  # noqa
        _log(f"running without app bundle ({e})")


def _build(support):
    src = os.path.realpath(sys.executable)
    app = os.path.join(support, "runtime", "Caption Remover.app")
    contents = os.path.join(app, "Contents")
    exe = os.path.join(contents, "MacOS", "Caption Remover")
    stamp_path = os.path.join(contents, "Resources", "stamp")
    icon_src = os.environ.get("CR_ICON")
    stamp = f"{RUNTIME_REV}|{src}|{os.path.getsize(src)}|{os.path.getmtime(src)}|{icon_src or ''}"
    try:
        if os.path.exists(exe) and open(stamp_path).read() == stamp:
            return exe
    except OSError:
        pass

    _log("building runtime bundle")
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    shutil.copy2(src, exe)
    os.chmod(exe, 0o755)
    os.symlink(os.path.join(sys.base_prefix, "lib"), os.path.join(contents, "lib"))
    if icon_src and os.path.exists(icon_src):
        shutil.copy2(icon_src, os.path.join(contents, "Resources", "AppIcon.icns"))
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump({
            "CFBundleName": "Caption Remover",
            "CFBundleDisplayName": "Caption Remover",
            "CFBundleExecutable": "Caption Remover",
            "CFBundleIdentifier": "com.trustudio.captionremover.engine",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        }, f)
    # re-sign so macOS accepts the copied interpreter inside its new bundle
    subprocess.run(["codesign", "--force", "--sign", "-", exe], capture_output=True)
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", app], capture_output=True)
    with open(stamp_path, "w") as f:
        f.write(stamp)
    return exe
