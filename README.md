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
- **Drop a video** (or click *Choose video*). It scans the clip and shows faint amber boxes where it found text.
- **Find & remove text** (on by default) catches subtitles, word-by-word social captions and titles automatically, on every frame.
- **Remove area:** drag a box over anything else, like logos, lower-third bars, stickers or emojis. Use *Starts here / Ends here* to limit a box to part of the clip. Drag a box to move it; Delete removes the selected one.
- **Text zone:** draw a zone so auto-removal only looks there (e.g. the bottom third). This leaves real text in the scene, like signs, shirts and product labels, untouched.
- **Preview result** renders the current frame so you can check before committing.
- **Fast** is a quick smudge fill for drafts. **Quality** is the AI fill (LaMa) for delivery.
- **Steady fill** reuses the patch on still shots so it doesn't flicker.
- **Export** as MP4 (H.264) or ProRes 422 HQ (.mov) for Premiere, with the original audio kept. Set in/out to process only part of a clip.

Finished files save to **Movies › Caption Remover**.

## Tips
- Big captions over busy, fast-moving shots are the hardest case. Check those spots, and patch in Premiere if needed.
- Raise **Edge padding** if you see leftover outline or shadow around removed text.
- Raise **Sensitivity** if small or faint text is missed; lower it if it's grabbing scene detail.
