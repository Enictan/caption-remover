#!/bin/bash
# Rebuilds "packaging/windows/Caption Remover.exe" (the Windows launcher/installer).
# Only needed when packaging/windows/launcher/main.go changes. Needs Go (and rsrc for the icon):
#   go install github.com/akavel/rsrc@latest
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="$(grep -m1 '^VERSION = ' "$ROOT/app.py" | cut -d'"' -f2)"
cd "$HERE/launcher"
RSRC="$(command -v rsrc || echo "$HOME/go/bin/rsrc")"
"$RSRC" -ico "$ROOT/packaging/icon/icon.ico" -arch amd64 -o rsrc_windows_amd64.syso
GOOS=windows GOARCH=amd64 go vet .
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-H=windowsgui -s -w -X main.version=$VERSION" -o "$HERE/Caption Remover.exe" .
echo "Built: $HERE/Caption Remover.exe (launcher v$VERSION)"
