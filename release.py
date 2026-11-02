"""
Publish a new version of Caption Remover.

    Release.command 1.0.2 "Faster preview, new export option"
    (or:  python release.py 1.0.2 "notes"   [--no-publish])

What it does:
  1. sets VERSION in app.py and writes update.json (where installed apps look for updates)
  2. builds  dist/caption-remover-app-<v>.zip   (code-only update, a few hundred KB)
             dist/Caption-Remover-Mac-<v>.zip    (full app, for fresh installs)
             dist/latest.json                    (update feed: version, url, checksum, notes)
  3. saves the code to the private repo (git commit + tag + push)
  4. creates a GitHub release on the public updates repo with those files
     -> every installed copy shows "Update available" on its next launch
Requires: git, and the GitHub CLI (gh) logged in:  brew install gh && gh auth login
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
CODE_FILES = ["app.py", "engine.py", "updater.py", "update.json", "requirements.txt", "static/index.html"]


def run(cmd, **kw):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, check=True, **kw)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    publish = "--no-publish" not in sys.argv
    if not args or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        sys.exit('Usage: release.py <version e.g. 1.0.2> "what changed"')
    ver, notes = args[0], (args[1] if len(args) > 1 else "")
    conf = json.load(open(CONF))
    owner, updates_repo = conf["owner"], conf["updates_repo"]

    # current version must go up
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    cur = re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1)
    if tuple(map(int, ver.split("."))) <= tuple(map(int, cur.split("."))):
        sys.exit(f"New version {ver} must be higher than current {cur}")

    print(f"Releasing Caption Remover {cur} -> {ver}")
    src = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{ver}"', src, count=1, flags=re.M)
    open(os.path.join(ROOT, "app.py"), "w", encoding="utf-8").write(src)
    feed = f"https://github.com/{owner}/{updates_repo}/releases/latest/download/latest.json"
    json.dump({"feed": feed}, open(os.path.join(ROOT, "update.json"), "w"), indent=2)

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
        "url": f"https://github.com/{owner}/{updates_repo}/releases/download/v{ver}/{upd_name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "notes": notes,
    }
    json.dump(latest, open(os.path.join(DIST, "latest.json"), "w"), indent=2)

    # 2) full Mac app for new installs
    run(["bash", "packaging/mac/build_app.sh", DIST])
    full = os.path.join(DIST, f"Caption-Remover-Mac-{ver}.zip")
    if os.path.exists(full):
        os.remove(full)
    shutil.copy(os.path.join(ROOT, "README.md"), os.path.join(DIST, "How to install.md"))
    subprocess.run(["zip", "-r", "-y", "-q", full, "Caption Remover.app", "How to install.md"], cwd=DIST, check=True)
    print(f"Built:\n  {upd}\n  {full}\n  {os.path.join(DIST, 'latest.json')}")

    if not publish:
        print("--no-publish: skipped git push and GitHub release")
        return

    # 3) save code to the private repo
    if os.path.isdir(os.path.join(ROOT, ".git")):
        run(["git", "add", "-A"])
        subprocess.run(["git", "commit", "-m", f"Release v{ver}" + (f": {notes}" if notes else "")], cwd=ROOT)
        run(["git", "tag", "-f", f"v{ver}"])
        run(["git", "push"])
        run(["git", "push", "-f", "origin", f"v{ver}"])

    # 4) publish to the public updates repo
    run(["gh", "release", "create", f"v{ver}", "-R", f"{owner}/{updates_repo}",
         "--title", f"Caption Remover {ver}", "--notes", notes or f"Caption Remover {ver}",
         upd, full, os.path.join(DIST, "latest.json")])
    print(f"\nPublished v{ver}. Installed apps will offer the update on their next launch.")


if __name__ == "__main__":
    main()
