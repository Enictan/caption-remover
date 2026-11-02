# Turning Caption Remover into a product

Current state (v1.0): Mac app for in-house use. It's a small app bundle (~300 KB) that downloads its own Python, ffmpeg and models on first launch.

## Code layout
- `engine.py`: detection + fill + video I/O. Platform-neutral; paths come from env vars (`CR_FFMPEG`, `CR_FFPROBE`, `CR_MODELS`, `CR_OUTPUT`).
- `app.py`: local API + native window (pywebview: WebKit on Mac, WebView2 on Windows).
- `static/index.html`: the whole UI.
- `packaging/mac/`: launcher, Info.plist, `build_app.sh` → `dist/Caption Remover.app`.
- `packaging/icon/`: icon source + `.icns` (Mac) and `.ico` (Windows).

## Steps to a sellable release
1. **Windows build.** Same app.py/engine.py. Package with PyInstaller (or a bootstrap launcher like the Mac one) and an Inno Setup installer using `icon.ico`. The engine already picks Windows' own H.264 encoder (`h264_mf`) and NVIDIA GPU (CUDA) when present. Needs a Windows machine or GitHub Actions `windows-latest` runner to build and test.
2. **Code signing.**
   - Mac: Apple Developer Program ($99/yr) → sign with a Developer ID certificate + notarize. This removes the "Open Anyway" step.
   - Windows: a code-signing certificate (OV/EV) so SmartScreen doesn't warn.
3. **Encoder licensing.** The free ffmpeg build used now includes x264 (GPL). For a closed-source paid product, ship an LGPL ffmpeg build without x264. The engine falls back automatically to Apple VideoToolbox (Mac) / Media Foundation (Windows) for H.264.
4. **Model licences.** LaMa (Apache-2.0) and EasyOCR/CRAFT (Apache-2.0) both allow commercial use. Keep their licence notices in an "About / Licences" screen.
5. **Self-contained installer.** Bundle Python + models inside the app instead of downloading on first run: bigger download (~1.5 GB), but it works offline and is easier to sign/notarize.
6. **Licence keys + payments.** e.g. Lemon Squeezy or Paddle (they handle global tax and issue keys), with a key check on launch.
7. **Auto-update.** Sparkle (Mac) / WinSparkle (Windows), or a simple "new version available" check.
8. **Quality upgrade.** Add ProPainter as an optional "Best" mode for camera moves (needs a strong GPU; heavier install).
