#!/bin/bash
# Assembles "Caption Remover.app" from the source tree. Works on macOS or Linux.
#   bash packaging/mac/build_app.sh [outdir]
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="$(python3 -c "import re;print(re.search(r'VERSION = \"([^\"]+)\"', open('$ROOT/app.py').read()).group(1))")"
APP="$OUT/Caption Remover.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app/static"

sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$(date +%Y%m%d%H%M)/" "$ROOT/packaging/mac/Info.plist" > "$APP/Contents/Info.plist"
cp "$ROOT/packaging/mac/launcher.sh" "$APP/Contents/MacOS/CaptionRemover"
chmod 755 "$APP/Contents/MacOS/CaptionRemover"
cp "$ROOT/packaging/icon/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$ROOT/app.py" "$ROOT/engine.py" "$ROOT/music.py" "$ROOT/updater.py" "$ROOT/macapp.py" "$ROOT/voice.py" "$ROOT/voice_worker.py" "$ROOT/requirements.txt" "$APP/Contents/Resources/app/"
[ -f "$ROOT/update.json" ] && cp "$ROOT/update.json" "$APP/Contents/Resources/app/"
cp "$ROOT/static/index.html" "$ROOT/static/voice.html" "$APP/Contents/Resources/app/static/"
mkdir -p "$APP/Contents/Resources/app/static/vendor" && cp "$ROOT/static/vendor/"*.js "$APP/Contents/Resources/app/static/vendor/"
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "Built: $APP  (v$VERSION)"
