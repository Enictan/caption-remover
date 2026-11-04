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
9. **Voice tools licences.** Voicebox is MIT. It's installed from a pinned commit (`voice.py: VOICEBOX_REF`) into `<support>/voice`. The voice models it downloads have their own licences (e.g. Qwen3-TTS Apache-2.0, Kokoro Apache-2.0, Chatterbox MIT, Whisper MIT); check each before selling, and keep the consent note on cloning. Windows: the same code runs (CUDA/DirectML via Voicebox); the voice engine setup needs a Python 3.12 there.

## Voice code layout
- `voice.py`: installs/starts the Voicebox server (own Python env), and runs transcribe, translate, clone, dub, fix-lines, voiceover and sound jobs. Uses the app's ffmpeg.
- `voice_worker.py`: runs inside the voice env: starts Voicebox with a parent watchdog, and does Whisper transcription with timestamps.
- `static/voice.html`: the Voice workspace (shown in a frame over the Clean UI).
10. **Music licences.** Demucs (MIT, weights from Meta), Signalsmith Stretch (MIT), librosa (ISC). pedalboard bundles Rubber Band (GPL) and is used for saved files; for a closed-source paid product either release that part under GPL, license Rubber Band commercially, or render saved files with Signalsmith instead. The "cleanest vocals" stem option uses BS-RoFormer (model code MIT, vendored in `roformer/` from python-audio-separator; weights `model_bs_roformer_ep_317_sdr_12.9755` from the UVR model repo, trained by viperx). The weights have no explicit commercial licence: ask the author or swap in a model with a clear licence before selling.

