# Caption Remover · Tru Studio

Removes burned-in captions, subtitles, titles, logos and other graphics from video. Runs entirely on your Mac.

## Install (Mac, Apple Silicon)
1. Unzip and drag **Caption Remover.app** into **Applications**.
2. Open it. macOS will block it the first time because it isn't signed by Apple yet:
   - **macOS 15 Sequoia or newer:** click *Done*, then go to **System Settings → Privacy & Security**, scroll down, click **Open Anyway** next to Caption Remover, and confirm.
   - **macOS 14 or older:** right-click the app → **Open** → **Open**.
3. Click **Set Up**. The one-time setup downloads about 1.5 GB and takes 5 to 15 minutes. A small window shows the progress; you can hide it.
4. The app opens in its own window. From then on it opens straight away.

Everything it downloads lives in `~/Library/Application Support/Caption Remover`. To uninstall, delete the app and that folder.
If something goes wrong, the log is at `~/Library/Logs/Caption Remover.log`.

## Using it
- **Open a video:** drop it on the window, click *Choose video*, or press ⌘O. Text is found automatically and shown in amber on the video and the timeline.
- **Area tool (A):** drag a box over anything else, like logos, lower thirds or stickers. Drag its ends on the timeline to set when it applies.
- **Text zone (Z):** limits automatic removal to one part of the frame, so real text in the scene (signs, labels) is left alone.
- **Compare (2):** before/after split on the current frame. **Result (3)** shows just the cleaned frame.
- **Remove captions (⌘E):** the gold button. Set In/Out (I, O) to process only part of the clip.
- **Settings & Help:** the ⚙ and ? icons in the toolbar. There you'll find the output folder, defaults, removal options, updates, a quick guide, shortcuts and app info.

Finished files save to **Movies › Caption Remover** (changeable in Settings).

## Voice (dub, captions, voiceovers)
Switch to **Voice** at the top left (or press **Voice it** after cleaning a video).
- **First time:** click **Set up voice engine**. It installs Voicebox, an open-source voice studio, privately inside the app's folder: about 6 GB, 15 to 40 minutes. Each voice model downloads the first time you use it.
- **Video & audio:** open a video or audio file → **Transcribe** → edit the lines →
  - **Captions:** export SRT, VTT or text (original or translated).
  - **Translate**, then **Dub the whole thing** in a cloned or chosen voice.
  - **Re-say edited lines:** change a few words and only those moments are replaced, in the same voice.
  - **Sound:** clean up noise, even out loudness, extract audio, add or replace a soundtrack (works without the voice engine).
- **Voiceover:** paste a script, pick a voice, save WAV or MP3.
- **Voices:** clone from a 5–25 s recording, describe a new voice, or add ready-made ones.

Voice results save to **Movies › Caption Remover › Voice**. Dubs also save a voice-only WAV for your edit.
Only clone voices you have permission to use.

## Tips
- Big captions over busy, fast-moving shots are the hardest case. Check those spots, and patch in Premiere if needed.
- Raise **Edge padding** if you see leftover outline or shadow around removed text.
- Raise **Sensitivity** if small or faint text is missed; lower it if it's grabbing scene detail.
