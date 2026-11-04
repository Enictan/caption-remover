"""
Publish a new version of Caption Remover.

    Release.command                        (asks for version + notes)
    python release.py 1.0.3 "notes"        [--no-publish]

What it does:
  1. sets VERSION in app.py and writes update.json (where installed apps look for updates)
  2. builds  dist/caption-remover-app-<v>.zip   (code-only update, shared by Mac and Windows)
             dist/Caption-Remover-Mac-<v>.zip    (full Mac app, for fresh installs)
             dist/Caption-Remover-Windows-<v>.zip (full Windows app, for fresh installs)
             dist/latest.json                    (update feed: version, url, checksum, notes)
  3. saves the code to the private repo   (git commit + tag + push)
  4. copies the update files into the public updates repo and pushes
     -> every installed copy shows "Update available" within a few minutes
Requires only git (already signed in to GitHub on this Mac).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
CONF = os.path.join(ROOT, "packaging", "release.json")
CODE_FILES = ["app.py", "engine.py", "music.py", "updater.py", "macapp.py", "voice.py", "voice_worker.py", "update.json",
              "requirements.txt", "static/index.html", "static/voice.html", "static/vendor/SignalsmithStretch.js",
              "roformer/__init__.py", "roformer/bs_roformer.py", "roformer/attend.py", "roformer/rotary.py",
              "roformer/device_utils.py", "roformer/LICENSE",
              "premiere/TruStudioLink/CSXS/manifest.xml", "premiere/TruStudioLink/index.html",
              "premiere/TruStudioLink/main.js", "premiere/TruStudioLink/host.jsx"]
KEEP_OLD = 5  # old versions kept in the updates repo


def run(cmd, cwd=ROOT, check=True):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check)


def raw(owner, repo, path):
    return f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    publish = "--no-publish" not in sys.argv
    if not args or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        sys.exit('Usage: release.py <version e.g. 1.0.3> "what changed"')
    ver, notes = args[0], (args[1] if len(args) > 1 else "")
    conf = json.load(open(CONF))
    owner, updates_repo = conf["owner"], conf["updates_repo"]
    updates_dir = os.path.abspath(os.path.join(ROOT, conf.get("updates_dir", "../caption-remover-updates")))

    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    cur = re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1)
    if tuple(map(int, ver.split("."))) <= tuple(map(int, cur.split("."))):
        sys.exit(f"New version {ver} must be higher than current {cur}")

    print(f"Releasing Caption Remover {cur} -> {ver}")
    src = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{ver}"', src, count=1, flags=re.M)
    open(os.path.join(ROOT, "app.py"), "w", encoding="utf-8").write(src)
    json.dump({"feed": raw(owner, updates_repo, "latest.json")},
              open(os.path.join(ROOT, "update.json"), "w"), indent=2)

    os.makedirs(DIST, exist_ok=True)
    # 1) code-only update package
    upd_name = f"caption-remover-app-{ver}.zip"
    upd = os.path.join(DIST, upd_name)
    with zipfile.ZipFile(upd, "w", zipfile.ZIP_DEFLATED) as z:
        for f in CODE_FILES:
            z.write(os.path.join(ROOT, f), f)
    data = open(upd, "rb").read()
    latest = {
        "version": ver,
        "url": raw(owner, updates_repo, f"releases/{upd_name}"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "notes": notes,
    }
    json.dump(latest, open(os.path.join(DIST, "latest.json"), "w"), indent=2)

    # 2) full Mac app for new installs
    run(["bash", "packaging/mac/build_app.sh", DIST])
    full_name = f"Caption-Remover-Mac-{ver}.zip"
    full = os.path.join(DIST, full_name)
    if os.path.exists(full):
        os.remove(full)
    shutil.copy(os.path.join(ROOT, "README.md"), os.path.join(DIST, "How to install.md"))
    run(["zip", "-r", "-y", "-q", full, "Caption Remover.app", "How to install.md"], cwd=DIST)
    # 3) full Windows app for new installs (uses the prebuilt launcher exe; rebuilt if Go is here)
    win_exe = os.path.join(ROOT, "packaging", "windows", "Caption Remover.exe")
    if shutil.which("go"):
        subprocess.run(["bash", "packaging/windows/build_launcher.sh"], cwd=ROOT)
    win = os.path.join(DIST, f"Caption-Remover-Windows-{ver}.zip")
    with zipfile.ZipFile(win, "w", zipfile.ZIP_DEFLATED) as z:
        top = "Caption Remover"
        z.write(win_exe, f"{top}/Caption Remover.exe")
        z.write(os.path.join(ROOT, "packaging", "icon", "icon.ico"), f"{top}/AppIcon.ico")
        z.write(os.path.join(ROOT, "packaging", "windows", "README.txt"), f"{top}/README.txt")
        for f in CODE_FILES:
            z.write(os.path.join(ROOT, f), f"{top}/app/{f}")
    print(f"Built:\n  {upd}\n  {full}\n  {win}\n  {os.path.join(DIST, 'latest.json')}")

    if not publish:
        print("--no-publish: skipped saving to GitHub")
        return

    # 4) save code to the private repo
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"Release v{ver}" + (f": {notes}" if notes else "")], check=False)
    run(["git", "tag", "-f", f"v{ver}"])
    run(["git", "push"])
    run(["git", "push", "-f", "origin", f"v{ver}"], check=False)  # tag; not critical

    # 5) publish to the public updates repo
    if not os.path.isdir(os.path.join(updates_dir, ".git")):
        run(["git", "clone", f"https://github.com/{owner}/{updates_repo}.git", updates_dir], cwd=os.path.dirname(updates_dir))
    run(["git", "pull", "--ff-only"], cwd=updates_dir, check=False)
    rel = os.path.join(updates_dir, "releases")
    os.makedirs(rel, exist_ok=True)
    shutil.copy(upd, rel)
    shutil.copy(full, rel)
    shutil.copy(full, os.path.join(rel, "Caption-Remover-Mac-latest.zip"))
    shutil.copy(win, rel)
    shutil.copy(win, os.path.join(rel, "Caption-Remover-Windows-latest.zip"))
    # prune old versions
    def vkey(n):
        m = re.search(r"(\d+)\.(\d+)\.(\d+)\.zip$", n)
        return tuple(map(int, m.groups())) if m else (0,)
    for prefix in ("caption-remover-app-", "Caption-Remover-Mac-", "Caption-Remover-Windows-"):
        olds = sorted([n for n in os.listdir(rel) if n.startswith(prefix) and "latest" not in n], key=vkey)
        for n in olds[:-KEEP_OLD]:
            os.remove(os.path.join(rel, n))
    json.dump(latest, open(os.path.join(updates_dir, "latest.json"), "w"), indent=2)  # last: flips the feed
    run(["git", "add", "-A"], cwd=updates_dir)
    run(["git", "commit", "-m", f"Caption Remover {ver}" + (f": {notes}" if notes else "")], cwd=updates_dir)
    run(["git", "push"], cwd=updates_dir)
    print(f"\nPublished v{ver}. Installed apps will offer the update within a few minutes.")


if __name__ == "__main__":
    main()
