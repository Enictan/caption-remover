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

## Music (two decks: compare, transpose, stems)
Switch to **Music** at the top.
- **Load songs:** drop a file onto Deck A or Deck B, or add several to the **Library** (right). The Library ranks songs by how well they fit Deck A; press **A** or **B** to load one.
- **Each deck plays the whole song.** Drag the big waveform to scrub, click the strip below it to jump. **CUE** sets a cue point when paused and jumps back to it while playing. « ‹ › » jump by 4 or 1 beats.
- **Key:** − / + shifts up to 12 semitones, live while it plays. **Match** picks the shift that fits the other deck. Click the detected key to correct it.
- **Tempo:** slider ±30% (or −/+ 0.1 BPM), live. If a tempo was detected at half or double speed, press ×2 or ×½.
- **Sync:** *Independent* lets each deck run at its own tempo; *Sync* locks the follower to the leader (choose A or B). **Align beats** lines the beats up. The status in the middle shows whether keys and tempos fit.
- **Stems:** **Split stems** separates vocals, drums, bass and other (or 6 stems with guitar and piano). Each stem has Mute, Solo and volume. The first split of a song takes a minute or two (the model downloads once, about 80 MB); after that it's instant. Splitting also re-checks the key without the drums, which is more accurate.
- **Save…** writes the deck as a WAV with your key, tempo and the stems you kept, to **Movies › Caption Remover › Music**.
- Keys: Space play/pause the selected deck, Shift+Space both, 1/2 select a deck, [ ] key, ← → beats, C cue.

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
