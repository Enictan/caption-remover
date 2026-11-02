#!/bin/bash
# Runs Caption Remover straight from this project folder, using the engine the installed
# app already set up. Edit the code, close the window, double-click this again.
# UI-only changes (static/index.html): right-click in the window > Reload.
cd "$(dirname "$0")"
SUP="$HOME/Library/Application Support/Caption Remover"
PY="$SUP/python/bin/python3"
if [ ! -x "$PY" ]; then
  echo "Open the installed Caption Remover app once first. It sets up the engine dev mode uses."
  read -n 1 -s -r -p "Press any key to close"; exit 1
fi
# install any new packages the code now needs
H=$(shasum requirements.txt | cut -c1-10)
if [ "$(cat "$SUP/.dev-req" 2>/dev/null)" != "$H" ]; then
  echo "Checking packages…"
  /usr/bin/arch -arm64 "$PY" -m pip install -q -r requirements.txt && echo "$H" > "$SUP/.dev-req"
fi
export CR_FFMPEG="$SUP/bin/ffmpeg" CR_FFPROBE="$SUP/bin/ffprobe" CR_MODELS="$SUP/models"
export CR_ICON="$PWD/packaging/icon/AppIcon.icns" CR_DEV=1 PYTORCH_ENABLE_MPS_FALLBACK=1
echo "Caption Remover DEV running from: $PWD"
echo "(close the app window to stop)"
exec /usr/bin/arch -arm64 "$PY" app.py
