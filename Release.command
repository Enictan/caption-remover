#!/bin/bash
# Publish a new version. Double-click, then answer the two questions.
cd "$(dirname "$0")"
PY="$HOME/Library/Application Support/Caption Remover/python/bin/python3"
[ -x "$PY" ] || PY=python3
if ! command -v gh >/dev/null 2>&1; then
  for b in /opt/homebrew/bin /usr/local/bin; do [ -x "$b/gh" ] && export PATH="$b:$PATH"; done
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "The GitHub CLI isn't installed. Run:  brew install gh  &&  gh auth login"
  read -n 1 -s -r -p "Press any key to close"; exit 1
fi
CUR=$(grep -m1 '^VERSION = ' app.py | cut -d'"' -f2)
echo "Current version: $CUR"
read -r -p "New version number (e.g. 1.0.3): " VER
read -r -p "What changed (one line, shown to users): " NOTES
"$PY" release.py "$VER" "$NOTES"
echo ""
read -n 1 -s -r -p "Press any key to close"
