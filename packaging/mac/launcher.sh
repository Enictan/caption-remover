#!/bin/bash
# Caption Remover.app launcher (Contents/MacOS/CaptionRemover)
# First launch: downloads its own Python, ffmpeg and AI models into
#   ~/Library/Application Support/Caption Remover   (nothing touches the system)
# Every launch after: starts the app straight away.
# Updates: the app downloads new code into  <support>/app ; this launcher runs whichever
#   is newer, the downloaded code or the code bundled inside the .app.

APP_NAME="Caption Remover"
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
BUNDLED="$RES/app"
SRC="$BUNDLED"
ICON="$RES/AppIcon.icns"
SUP="$HOME/Library/Application Support/Caption Remover"
LOG="$HOME/Library/Logs/Caption Remover.log"
PY="$SUP/python/bin/python3"
STAMP="$SUP/.ready"

PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7%2B20241016-aarch64-apple-darwin-install_only.tar.gz"
FF_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0"
LAMA_URL="https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
CRAFT_URL="https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip"

# If macOS started us under Rosetta (Intel emulation) on an Apple Silicon Mac,
# restart natively so Python, PyTorch and the Apple GPU run as arm64.
if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ] && [ -z "$CR_NATIVE_RELAUNCH" ]; then
  export CR_NATIVE_RELAUNCH=1
  exec /usr/bin/arch -arm64 /bin/bash "$0" "$@"
fi

mkdir -p "$SUP" "$(dirname "$LOG")"

# ---------- pick the code to run: downloaded update if it's newer than the bundle ----------
ver() { grep -m1 '^VERSION = ' "$1/app.py" 2>/dev/null | cut -d'"' -f2; }
newer() {  # is version $1 greater than $2 ?
  [ -n "$1" ] && [ "$1" != "$2" ] && \
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" = "$1" ]
}
if [ -f "$SUP/app/app.py" ] && newer "$(ver "$SUP/app")" "$(ver "$BUNDLED")"; then
  SRC="$SUP/app"
fi
# Setup re-runs only when the building blocks change (requirements or download sources)
WANT="deps-$( (cat "$SRC/requirements.txt"; printf '%s\n' "$PY_URL" "$FF_BASE" "$LAMA_URL" "$CRAFT_URL") | shasum | cut -c1-10)"
echo "=== $(date) launch v$(ver "$SRC") from $SRC $WANT (arch: $(uname -m), translated: $(sysctl -n sysctl.proc_translated 2>/dev/null)) ===" >> "$LOG"

# ---------- small native dialogs ----------
DIALOG_PID=""
status() {
  [ -n "$DIALOG_PID" ] && kill "$DIALOG_PID" 2>/dev/null
  osascript - "$1" "$ICON" >/dev/null 2>&1 <<'AS' &
on run argv
  display dialog (item 1 of argv) with title "Caption Remover setup" buttons {"Hide"} default button 1 with icon (POSIX file (item 2 of argv) as alias) giving up after 7200
end run
AS
  DIALOG_PID=$!
}
close_status() { [ -n "$DIALOG_PID" ] && kill "$DIALOG_PID" 2>/dev/null; DIALOG_PID=""; }
ask() {  # returns 0 if user pressed the OK button
  osascript - "$1" "$2" "$ICON" >/dev/null 2>&1 <<'AS'
on run argv
  display dialog (item 1 of argv) with title "Caption Remover" buttons {"Cancel", (item 2 of argv)} default button 2 cancel button 1 with icon (POSIX file (item 3 of argv) as alias)
end run
AS
}
fail() {
  close_status
  echo "FAILED: $1" >> "$LOG"
  if osascript - "$1" "$ICON" >/dev/null 2>&1 <<'AS'
on run argv
  display dialog ("Setup couldn't finish: " & (item 1 of argv) & return & return & "Check your internet connection and open the app again. It will continue where it stopped.") with title "Caption Remover" buttons {"Close", "Show log"} default button 1 cancel button 1 with icon (POSIX file (item 2 of argv) as alias)
end run
AS
  then open -a Console "$LOG"; fi
  exit 1
}
get() {  # url dest
  curl -L --fail --retry 3 --retry-delay 2 -sS -o "$2.part" "$1" >> "$LOG" 2>&1 && mv "$2.part" "$2"
}

# ---------- first-run / update setup ----------
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" != "1" ]; then
    osascript -e 'display alert "Caption Remover needs an Apple Silicon Mac (M1 or newer)."' >/dev/null 2>&1
    exit 1
  fi
  if [ ! -x "$PY" ]; then
    ask "Caption Remover needs a one-time setup.

It downloads about 1.5 GB (AI models and video tools) and takes 5 to 15 minutes, depending on your connection. Everything stays inside the app's own folder." "Set Up" || exit 0
  fi

  if [ ! -x "$PY" ]; then
    status "Setting up Caption Remover (1 of 4)

Downloading Python…"
    get "$PY_URL" "$SUP/python.tar.gz" || fail "Python download"
    rm -rf "$SUP/python"
    tar -xzf "$SUP/python.tar.gz" -C "$SUP" >> "$LOG" 2>&1 || fail "Python unpack"
    rm -f "$SUP/python.tar.gz"
  fi

  mkdir -p "$SUP/bin"
  if [ ! -x "$SUP/bin/ffmpeg" ] || [ ! -x "$SUP/bin/ffprobe" ]; then
    status "Setting up Caption Remover (2 of 4)

Downloading video tools (ffmpeg)…"
    get "$FF_BASE/ffmpeg-darwin-arm64" "$SUP/bin/ffmpeg" || fail "ffmpeg download"
    get "$FF_BASE/ffprobe-darwin-arm64" "$SUP/bin/ffprobe" || fail "ffprobe download"
    chmod +x "$SUP/bin/ffmpeg" "$SUP/bin/ffprobe"
    xattr -dr com.apple.quarantine "$SUP/bin" 2>/dev/null
  fi

  status "Setting up Caption Remover (3 of 4)

Installing the AI engine. This is the long step…"
  /usr/bin/arch -arm64 "$PY" -m pip install --upgrade pip -q >> "$LOG" 2>&1
  /usr/bin/arch -arm64 "$PY" -m pip install -r "$SRC/requirements.txt" >> "$LOG" 2>&1 || fail "AI engine install"

  mkdir -p "$SUP/models"
  if [ ! -f "$SUP/models/big-lama.pt" ] || [ ! -f "$SUP/models/craft_mlt_25k.pth" ]; then
    status "Setting up Caption Remover (4 of 4)

Downloading AI models (about 300 MB)…"
    [ -f "$SUP/models/big-lama.pt" ] || get "$LAMA_URL" "$SUP/models/big-lama.pt" || fail "fill model download"
    if [ ! -f "$SUP/models/craft_mlt_25k.pth" ]; then
      get "$CRAFT_URL" "$SUP/models/craft.zip" || fail "text detector download"
      (cd "$SUP/models" && unzip -o -q craft.zip && rm -f craft.zip) >> "$LOG" 2>&1 || fail "text detector unpack"
    fi
  fi

  echo "$WANT" > "$STAMP"
  close_status
  osascript -e 'display notification "Setup complete. Opening Caption Remover." with title "Caption Remover"' >/dev/null 2>&1
fi

# ---------- run ----------
export CR_FFMPEG="$SUP/bin/ffmpeg"
export CR_FFPROBE="$SUP/bin/ffprobe"
export CR_MODELS="$SUP/models"
export CR_ICON="$ICON"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export CR_SUPPORT="$SUP"
export CR_BUNDLE="$(cd "$RES/../.." && pwd)"
cd "$SRC" || fail "app files missing"
START=$(date +%s)
/usr/bin/arch -arm64 "$PY" app.py >> "$LOG" 2>&1
RC=$?
# Safety net: if a downloaded update crashes straight away, set it aside and run the bundled version
if [ $RC -ne 0 ] && [ "$SRC" = "$SUP/app" ] && [ $(( $(date +%s) - START )) -lt 20 ]; then
  echo "Update v$(ver "$SRC") failed to start (rc=$RC); falling back to bundled version" >> "$LOG"
  rm -rf "$SUP/app.broken"; mv "$SUP/app" "$SUP/app.broken"
  osascript -e 'display notification "The latest update could not start, so the previous version was opened." with title "Caption Remover"' >/dev/null 2>&1
  exec /bin/bash "$0" "$@"
fi
exit $RC
