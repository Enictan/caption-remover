# How to change and ship Caption Remover

## The loop
1. **Change something.** Ask Claude (in Claude Code, opened on this folder) or edit the files yourself.
2. **Try it:** double-click **Dev Mode.command**. It runs the app straight from this folder using the engine your installed app already set up.
   - Interface-only change (`static/index.html`): right-click in the window → **Reload**.
   - Engine/app change (`.py` files): close the window, double-click Dev Mode again.
3. **Ship it:** double-click **Release.command**, type the new version (e.g. 1.0.3) and one line about what changed.
   It saves the code to your private GitHub repo and publishes the update.
4. **Your installed app** (and anyone else's) shows "Version 1.0.3 is available" on next launch → **Update now** → **Restart now**. Done in seconds.

## Version numbers
- `1.0.x`: fixes and small tweaks
- `1.x.0`: new features
- Always go up; the release script refuses to publish a lower or equal number.

## What updates can't change by themselves
The launcher inside the .app (first-run setup, Rosetta fix) only changes when you install a fresh app. That's rare. Release.command also builds the full app zip (`dist/Caption-Remover-Mac-<version>.zip`) for those cases and for new installs.

## Safety nets
- Every download is checksum-verified; a broken download is rejected and the current version keeps running.
- If a new version crashes on start, the app automatically falls back to the previous version and won't offer that broken version again.
- Every release is tagged in Git, so any version can be restored.

## One-time setup on this Mac
- `brew install gh` then `gh auth login` (lets Release.command publish to GitHub).
- `packaging/release.json` holds your GitHub username and the updates repo name.
