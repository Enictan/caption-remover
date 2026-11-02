"""
Music Match: tempo + key detection, compatibility scoring, and transpose / tempo-matched previews.

- analyse(): decodes with ffmpeg, finds tempo + beat grid (librosa) and key (chroma vs. key profiles).
- match(): how a candidate fits the main track: the smallest transpose that lands it on the same
  or relative key, the tempo stretch needed (allowing half / double time), and a 0-100 fit score.
- render(): a section of a track, time-stretched and pitch-shifted with Rubber Band (pedalboard).
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import subprocess
import threading

import numpy as np

import engine  # ffmpeg paths + no-window flags

SR = 44100
AN_SR = 22050
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Camelot wheel numbers for major / minor roots (C major = 8B, A minor = 8A)
CAM_MAJ = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
CAM_MIN = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}
# Krumhansl-Kessler key profiles
KK_MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_lock = threading.Lock()
_cache: dict[str, np.ndarray] = {}   # decoded audio (stereo, SR) by path


# ------------------------------------------------------------------ decoding
def decode(path: str, sr: int = SR, mono: bool = False, max_sec: float | None = None) -> np.ndarray:
    """float32 array: (channels, samples)."""
    ch = 1 if mono else 2
    cmd = [engine.FFMPEG, "-v", "error", "-i", path, "-vn"]
    if max_sec:
        cmd += ["-t", f"{max_sec:.2f}"]
    cmd += ["-ac", str(ch), "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, **engine._NOWIN).stdout
    a = np.frombuffer(raw, np.float32)
    if a.size == 0:
        raise RuntimeError("No audio found in this file")
    return a.reshape(-1, ch).T.copy()


def audio(path: str) -> np.ndarray:
    with _lock:
        a = _cache.get(path)
    if a is None:
        a = decode(path)
        with _lock:
            if len(_cache) > 6:
                _cache.pop(next(iter(_cache)))
            _cache[path] = a
    return a


def duration(path: str) -> float:
    return audio(path).shape[1] / SR


# ------------------------------------------------------------------ analysis
FLATS_MINOR = {1: "Db", 3: "Eb", 8: "Ab", 10: "Bb"}


def _is_minor(mode) -> bool:
    return mode is True or mode == "minor"


def key_name(tonic: int, mode) -> str:
    m = _is_minor(mode)
    n = FLATS_MINOR.get(tonic % 12, NAMES[tonic % 12]) if m else NAMES[tonic % 12]
    return f"{n} {'minor' if m else 'major'}"


def camelot(tonic: int, mode) -> str:
    m = _is_minor(mode)
    return f"{(CAM_MIN if m else CAM_MAJ)[tonic % 12]}{'A' if m else 'B'}"


def _relative(tonic, mode):
    return ((tonic + 3) % 12, "major") if _is_minor(mode) else ((tonic - 3) % 12, "minor")


def shifted_key(t: dict, semis: int) -> dict:
    tonic = (t["tonic"] + int(semis)) % 12
    return {"tonic": tonic, "mode": t["mode"], "key": key_name(tonic, t["mode"]), "camelot": camelot(tonic, t["mode"])}


def nearest_beat(beats, t: float) -> float:
    return _snap(t, beats)


def _chroma(y, fmin, octaves):
    import librosa
    c = librosa.feature.chroma_cqt(y=y, sr=AN_SR, hop_length=2048, fmin=librosa.note_to_hz(fmin),
                                   n_octaves=octaves, bins_per_octave=36)
    return np.log1p(10 * c / (c.max(0, keepdims=True) + 1e-9))   # per-frame compression


def _key(y, beats=None):
    """Key from harmony above the sub-bass (kick drums fool chroma), then major vs relative minor
    from where the bass rests: overall, and at the very start and end of the song."""
    import librosa
    harm = librosa.effects.harmonic(y, margin=3.0)
    prof = _chroma(harm, "C2", 5).mean(1)
    scores = []
    for minor, kp in ((False, KK_MAJ), (True, KK_MIN)):
        for r in range(12):
            scores.append((float(np.corrcoef(prof, np.roll(kp, r))[0, 1]), r, minor))
    scores.sort(reverse=True)
    best = scores[0]
    # relative pair: same notes, different tonic
    maj_root = best[1] if not best[2] else (best[1] + 3) % 12
    min_root = (maj_root - 3) % 12
    import librosa as _lr
    harm_b = _lr.effects.harmonic(y, margin=8.0)      # stricter: drums out of the bass
    bass = _chroma(harm_b, "C2", 2)                   # ~65-260 Hz: above kick drums, where bass lines sit
    n = bass.shape[1]
    fr = lambda t: int(t * AN_SR / 2048)
    if beats is not None and len(beats) > 8:
        b = np.asarray(beats)
        first = bass[:, fr(b[0]):max(fr(b[0]) + 1, fr(b[2]))]                 # first chord (2 beats)
        last = bass[:, fr(b[-3]):max(fr(b[-3]) + 1, fr(b[-1]) + 1)]            # last chord
    else:
        first, last = bass[:, :max(1, n // 20)], bass[:, -max(1, n // 20):]
    w = bass.mean(1) + 1.0 * first.mean(1) + 0.6 * last.mean(1)
    h = prof
    tonic_maj = w[maj_root] + 0.5 * h[maj_root]
    tonic_min = w[min_root] + 0.5 * h[min_root]
    corr = {(r, m): c for c, r, m in scores}
    # combine profile fit with the bass/tonic evidence
    s_maj = corr[(maj_root, False)] + 0.35 * (tonic_maj - tonic_min) / (tonic_maj + tonic_min + 1e-9)
    s_min = corr[(min_root, True)] + 0.35 * (tonic_min - tonic_maj) / (tonic_maj + tonic_min + 1e-9)
    minor = s_min > s_maj
    root = min_root if minor else maj_root
    # confidence: how clearly this note set beats other note sets, and how clear major/minor is
    other = next(c for c, r, m in scores if r not in (maj_root, min_root) or (r == maj_root and m) or (r == min_root and not m))
    notes_conf = float(np.clip((max(s_maj, s_min) - other) * 5 + 0.35, 0, 1))
    mode_conf = float(np.clip(abs(s_maj - s_min) * 8, 0, 1))
    alt = key_name(maj_root, False) if minor else key_name(min_root, True)
    # next most likely different note set (for the "could also be" menu)
    nxt = next(((r, m) for c, r, m in scores if r not in (maj_root, min_root)), None)
    return root, minor, notes_conf, mode_conf, alt, nxt


def _tempo(y):
    import librosa
    onset = librosa.onset.onset_strength(y=y, sr=AN_SR, hop_length=512, aggregate=np.median)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=AN_SR, hop_length=512, units="time", start_bpm=110)
    bpm = float(np.atleast_1d(tempo)[0])
    beats = np.asarray(beats, float)
    if len(beats) > 8:                       # refine: straight line through the beat grid
        k = np.arange(len(beats))
        d = np.diff(beats)
        ok = np.abs(d - np.median(d)) < 0.25 * np.median(d)
        if ok.sum() > 6:
            idx = np.concatenate([[True], ok])
            slope = np.polyfit(k[idx], beats[idx], 1)[0]
            if slope > 0:
                bpm = 60.0 / slope
    # gather tempo confidence from how regular the beats are
    conf = 0.5
    if len(beats) > 8:
        d = np.diff(beats)
        conf = float(np.clip(1 - np.std(d) / (np.mean(d) + 1e-9) * 4, 0, 1))
    return bpm, beats, conf


def analyse(path: str) -> dict:
    y = decode(path, AN_SR, mono=True, max_sec=240)[0]
    dur = duration(path)
    bpm, beats, tconf = _tempo(y)
    root, minor, kconf, mconf, alt, nxt = _key(y, beats)
    mode = "minor" if minor else "major"
    rt, rm = _relative(root, mode)
    alts = [{"tonic": rt, "mode": rm, "key": key_name(rt, rm) + " (same notes)", "camelot": camelot(rt, rm)}]
    if nxt:
        nm = "minor" if nxt[1] else "major"
        alts.append({"tonic": nxt[0], "mode": nm, "key": key_name(nxt[0], nm), "camelot": camelot(nxt[0], nm)})
    # Major vs relative minor on a looping progression is genuinely ambiguous, so never claim
    # "confident" on that alone: the note set (what decides the transpose) is the reliable part.
    conf = min(kconf, 0.45 + 0.2 * mconf)          # at most 'fairly sure' until you confirm it
    out = {
        "bpm": round(bpm, 1), "tempo_conf": round(tconf, 2),
        "tonic": root, "mode": mode, "key": key_name(root, mode), "camelot": camelot(root, mode),
        "key_conf": round(conf, 2), "key_alts": alts, "duration": round(dur, 2),
        "beats": [round(float(b), 3) for b in beats[:4000]],
        "hot_start": hot_start(path, 30.0, beats),
    }
    out.update(_tags(path))
    return out


def _tags(path):
    """Title / artist from the file's tags, if any."""
    import json
    try:
        r = subprocess.run([engine.FFPROBE, "-v", "error", "-print_format", "json", "-show_format", path],
                           capture_output=True, text=True, **engine._NOWIN)
        tags = {k.lower(): v for k, v in (json.loads(r.stdout).get("format", {}).get("tags") or {}).items()}
        return {k: tags[k].strip() for k in ("title", "artist") if tags.get(k, "").strip()}
    except Exception:
        return {}


def hot_start(path: str, length: float, beats) -> float:
    """Start of the loudest `length`-second stretch (usually the chorus), snapped to a beat."""
    a = audio(path).mean(0)
    hop = SR // 4
    n = len(a) // hop
    if n < 2:
        return 0.0
    rms = np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(1))
    w = max(1, int(length * 4))
    if n <= w:
        return 0.0
    c = np.convolve(rms, np.ones(w), "valid")
    t = float(np.argmax(c)) / 4
    return round(_snap(t, beats), 3)


def waveform(path: str, bins: int = 800) -> list[float]:
    a = np.abs(audio(path)).max(0)
    n = len(a) // bins
    if n < 1:
        return [float(x) for x in a]
    p = a[: n * bins].reshape(bins, n).max(1)
    p = p / (p.max() + 1e-9)
    return [round(float(x), 3) for x in p]


# ------------------------------------------------------------------ matching
def _wrap(s):                     # semitones into -6..+5
    return ((s + 6) % 12) - 6


def shifts_to_fit(main: dict, cand: dict) -> list[dict]:
    """Transposes (-5..+5) that land the candidate on a compatible key, best first.
    same: main's key · relative: same notes as main, other mode · neighbour: one step round the Camelot wheel."""
    mt, mm, ct, cm = main["tonic"], main["mode"], cand["tonic"], cand["mode"]
    base = mt if _is_minor(cm) == _is_minor(mm) else _relative(mt, mm)[0]
    kind0 = "same" if _is_minor(cm) == _is_minor(mm) else "relative"
    targets = [(base, kind0), ((base + 7) % 12, "neighbour"), ((base + 5) % 12, "neighbour")]
    out = []
    for tr, kind in targets:
        for s in sorted({_wrap(tr - ct), _wrap(tr - ct) + 12, _wrap(tr - ct) - 12}, key=abs):
            if -5 <= s <= 5:
                out.append({"semitones": s, "kind": kind})
                break
    out.sort(key=lambda d: abs(d["semitones"]) + (1.5 if d["kind"] == "neighbour" else 0))
    return out


def tempo_fit(main_bpm: float, cand_bpm: float) -> dict:
    best = None
    for mult, feel in ((1, "normal"), (2, "double time"), (0.5, "half time")):
        eff = cand_bpm * mult
        ratio = main_bpm / eff            # playback speed for the candidate
        if best is None or abs(ratio - 1) < abs(best["ratio"] - 1):
            best = {"ratio": ratio, "feel": feel}
    return best


def match(main: dict, cand: dict) -> dict:
    shifts = shifts_to_fit(main, cand)
    best = shifts[0] if shifts else {"semitones": 0, "kind": "none"}
    tf = tempo_fit(main["bpm"], cand["bpm"])
    stretch = (tf["ratio"] - 1) * 100
    s = best["semitones"]
    key_pen = abs(s) * 9 + {"neighbour": 6, "relative": 2}.get(best["kind"], 0) + (25 if best["kind"] == "none" else 0)
    conf = 0.5 * (main.get("key_conf", .5) + cand.get("key_conf", .5))
    fit = max(0, min(100, round(100 - key_pen - abs(stretch) * 2.6 - (1 - conf) * 6)))
    res = shifted_key(cand, s)["key"]
    if best["kind"] == "same":
        why = f"lands on {res}, the same key as your main song" if s else f"already in {res}, the same key as your main song"
    elif best["kind"] == "relative":
        why = f"{'lands on' if s else 'already in'} {res}, which uses the same notes as {main['key']}"
    elif best["kind"] == "neighbour":
        why = f"{'lands on' if s else 'already in'} {res}, a neighbouring key that blends smoothly"
    else:
        why = "no close key within 5 semitones"
    return {"fit": fit, "shift": s, "kind": best["kind"], "why": why,
            "alternatives": [d["semitones"] for d in shifts[1:]],
            "tempo_ratio": round(tf["ratio"], 5), "stretch_pct": round(stretch, 2), "feel": tf["feel"]}


# ------------------------------------------------------------------ rendering
def _snap(t, beats):
    if beats is None or len(beats) == 0:
        return float(t)
    b = np.asarray(beats)
    return float(b[np.argmin(np.abs(b - t))])


def render(src: str, dst: str, start: float = 0.0, length: float | None = 30.0, semis: float = 0, ratio: float = 1.0,
           fade: bool = True) -> str:
    """Write `length` seconds of OUTPUT (None = to the end) starting at `start` in the source, sped up by
    `ratio` (1.04 = 4 % faster) and transposed by `semis`, as a WAV file (24-bit for full-song saves)."""
    import soundfile as sf
    a = audio(src)
    i0 = max(0, int(start * SR))
    if length is None:
        i1 = a.shape[1]
    else:
        i1 = min(a.shape[1], i0 + int(length * ratio * SR) + SR // 10)
    seg = np.ascontiguousarray(a[:, i0:i1])
    if abs(ratio - 1) > 1e-4 or abs(semis) > 1e-4:
        from pedalboard import time_stretch
        seg = time_stretch(seg, SR, stretch_factor=float(ratio), pitch_shift_in_semitones=float(semis),
                           high_quality=True, transient_mode="crisp", preserve_formants=True)
    if length is not None:
        seg = seg[:, : int(length * SR)]
    seg = np.ascontiguousarray(seg, dtype=np.float32)
    if fade:
        f = min(seg.shape[1] // 4, int(0.012 * SR))
        if f > 0:
            r = np.linspace(0, 1, f, dtype=np.float32)
            seg[:, :f] *= r
            seg[:, -f:] *= r[::-1]
    tmp = dst + ".part.wav"
    sf.write(tmp, np.clip(seg.T, -1, 1), SR, subtype="PCM_16" if fade else "PCM_24")
    os.replace(tmp, dst)
    return dst


def file_id(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:12]
