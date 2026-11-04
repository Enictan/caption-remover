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
# Temperley (Kostka-Payne) profiles: a second opinion that is less biased towards the tonic triad
TP_MAJ = np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0])
TP_MIN = np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0])
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


def _key(y, beats=None, harm=None, bass_sig=None):
    """Key from harmony above the sub-bass (kick drums fool chroma), then major vs relative minor
    from where the bass rests: overall, and at the very start and end of the song.
    With stems: `harm` = vocals + bass + other (no drums), `bass_sig` = the bass stem."""
    import librosa
    if harm is None:
        harm = librosa.effects.harmonic(y, margin=3.0)
    prof = _chroma(harm, "C2", 5).mean(1)
    scores = []
    for minor, kk, tp in ((False, KK_MAJ, TP_MAJ), (True, KK_MIN, TP_MIN)):
        for r in range(12):
            c = 0.5 * np.corrcoef(prof, np.roll(kk, r))[0, 1] + 0.5 * np.corrcoef(prof, np.roll(tp, r))[0, 1]
            scores.append((float(c), r, minor))
    scores.sort(reverse=True)
    best = scores[0]
    # relative pair: same notes, different tonic
    maj_root = best[1] if not best[2] else (best[1] + 3) % 12
    min_root = (maj_root - 3) % 12
    if bass_sig is not None:                          # a clean bass stem: use its whole range
        bass = _chroma(bass_sig, "C1", 3)
    else:
        harm_b = librosa.effects.harmonic(y, margin=8.0)  # stricter: drums out of the bass
        bass = _chroma(harm_b, "C2", 2)               # ~65-260 Hz: above kick drums, where bass lines sit
    n = bass.shape[1]
    fr = lambda t: int(t * AN_SR / 2048)
    if beats is not None and len(beats) > 8:
        b = np.asarray(beats)
        first = bass[:, fr(b[0]):max(fr(b[0]) + 1, fr(b[2]))]                 # first chord (2 beats)
        last = bass[:, fr(b[-3]):max(fr(b[-3]) + 1, fr(b[-1]) + 1)]            # last chord
    else:
        first, last = bass[:, :max(1, n // 20)], bass[:, -max(1, n // 20):]
    w = bass.mean(1) + 1.0 * first.mean(1) + 0.6 * last.mean(1)
    if beats is not None and len(beats) > 40:
        # songs come home at phrase starts: bass on the first beat of every 4-bar phrase
        b = np.asarray(beats)
        cols = [bass[:, fr(b[i]):max(fr(b[i]) + 1, fr(b[min(i + 1, len(b) - 1)]))].mean(1) for i in range(0, len(b) - 1, 16)]
        if cols:
            w = w + 1.5 * np.mean(cols, 0)
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
    y = decode(path, AN_SR, mono=True, max_sec=900)[0]
    dur = duration(path)
    bpm, beats, tconf = _tempo(y)
    root, minor, kconf, mconf, alt, nxt = _key(y, beats)
    out = _key_fields(root, minor, kconf, mconf, nxt)
    out.update({
        "bpm": round(bpm, 1), "tempo_conf": round(tconf, 2), "duration": round(dur, 2),
        "beats": [round(float(b), 3) for b in beats[:20000]],
        "hot_start": hot_start(path, 30.0, beats), "key_source": "mix",
    })
    out.update(_tags(path))
    return out


def _key_fields(root, minor, kconf, mconf, nxt, cap=True):
    mode = "minor" if minor else "major"
    rt, rm = _relative(root, mode)
    alts = [{"tonic": rt, "mode": rm, "key": key_name(rt, rm) + " (same notes)", "camelot": camelot(rt, rm)}]
    if nxt:
        nm = "minor" if nxt[1] else "major"
        alts.append({"tonic": nxt[0], "mode": nm, "key": key_name(nxt[0], nm), "camelot": camelot(nxt[0], nm)})
    # Major vs relative minor on a looping progression is genuinely ambiguous, so never claim
    # "confident" on that alone: the note set (what decides the transpose) is the reliable part.
    conf = min(kconf, 0.45 + 0.2 * mconf) if cap else min(kconf, 0.5 + 0.35 * mconf)
    return {"tonic": root, "mode": mode, "key": key_name(root, mode), "camelot": camelot(root, mode),
            "key_conf": round(conf, 2), "key_alts": alts}


def key_from_stems(stems: dict, beats) -> dict:
    """Re-detect the key with drums removed and a clean bass line for the tonic."""
    harm = None
    for name, p in stems.items():
        if name == "drums":
            continue
        a = decode(p, AN_SR, mono=True, max_sec=900)[0]
        harm = a if harm is None else harm[: len(a)] + a[: len(harm)]
    bass = decode(stems["bass"], AN_SR, mono=True, max_sec=900)[0] if "bass" in stems else None
    root, minor, kconf, mconf, alt, nxt = _key(None, beats, harm=harm, bass_sig=bass)
    out = _key_fields(root, minor, kconf, mconf, nxt, cap=False)
    out["key_source"] = "stems"
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


def peaks(path: str, per_sec: int = 50) -> dict:
    """Waveform for the decks: overall peak plus low / mid / high energy, `per_sec` columns a second."""
    import librosa
    y = decode(path, AN_SR, mono=True, max_sec=900)[0]
    hop = AN_SR // per_sec
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop, center=True))
    f = librosa.fft_frequencies(sr=AN_SR, n_fft=2048)
    bands = [(f < 250), (f >= 250) & (f < 4000), (f >= 4000)]
    out = {}
    for name, sel in zip(("low", "mid", "high"), bands):
        e = np.sqrt((S[sel] ** 2).sum(0))
        out[name] = e
    n = min(len(out["low"]), len(y) // hop + 1)
    pk = np.abs(y[: n * hop]).reshape(-1, hop).max(1) if n * hop <= len(y) else np.abs(np.pad(y, (0, n * hop - len(y)))).reshape(-1, hop).max(1)
    norm = lambda a: np.round(a[:n] / (np.percentile(a[:n], 99.5) + 1e-9), 3).clip(0, 1)
    return {"per_sec": per_sec, "peak": norm(pk).tolist(), "low": norm(out["low"]).tolist(),
            "mid": norm(out["mid"]).tolist(), "high": norm(out["high"]).tolist()}


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


# ------------------------------------------------------------------ stems (Demucs)
STEM_MODELS = {
    "htdemucs": {"label": "4 stems", "stems": ["vocals", "drums", "bass", "other"]},
    "htdemucs_6s": {"label": "6 stems (adds guitar & piano)", "stems": ["vocals", "drums", "bass", "guitar", "piano", "other"]},
    "htdemucs_ft": {"label": "4 stems, best quality (about 4x slower)", "stems": ["vocals", "drums", "bass", "other"]},
    "roformer": {"label": "4 stems, cleanest vocals (one-time 640 MB download)", "stems": ["vocals", "drums", "bass", "other"]},
}
# BS-RoFormer vocal model (MIT code, weights from the UVR model repo). Vocals from RoFormer, the rest from Demucs
# run on what is left once the vocals are taken out.
ROFORMER = {
    "ckpt": ("https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt",
             "5b84f37e8d444c8cb30c79d77f613a41c05868ff9c9ac6c7049c00aefae115aa", 639331213),
    "chunk": 352800, "overlap": 2,
    "model": dict(dim=512, depth=12, stereo=True, num_stems=1, time_transformer_depth=1, freq_transformer_depth=1,
                  freqs_per_bands=(2,) * 24 + (4,) * 12 + (12,) * 8 + (24,) * 8 + (48,) * 8 + (128, 129),
                  dim_head=64, heads=8, attn_dropout=0.0, ff_dropout=0.0, flash_attn=True, dim_freqs_in=1025,
                  stft_n_fft=2048, stft_hop_length=441, stft_win_length=2048, stft_normalized=False, mask_estimator_depth=2),
}
_models = {}
_mlock = threading.Lock()


def _torch_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def content_id(path: str) -> str:
    """Same song file -> same id, wherever it was loaded from (for the stems cache)."""
    h = hashlib.sha1()
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(size - (1 << 20))
            h.update(f.read(1 << 20))
    h.update(str(size).encode())
    return h.hexdigest()[:16]


STEMS_VER = "v2"   # bump to make every song split again (v2: GPU output checked against the CPU)


def stems_dir(cache_root: str, path: str, model: str) -> str:
    return os.path.join(cache_root, content_id(path), f"{model}-{STEMS_VER}")


def cached_stems(cache_root: str, path: str, model: str):
    d = stems_dir(cache_root, path, model)
    names = STEM_MODELS[model]["stems"]
    files = {n: os.path.join(d, n + ".flac") for n in names}
    return files if all(os.path.exists(p) for p in files.values()) else None


def _load_model(name, models_dir):
    with _mlock:
        if name not in _models:
            import torch
            os.environ.setdefault("TORCH_HOME", models_dir)
            torch.hub.set_dir(os.path.join(models_dir, "hub"))
            from demucs.pretrained import get_model
            m = get_model(name)
            m.eval()
            _models[name] = m
    return _models[name]


def _download(url, dst, sha, size, progress, lo, hi, what):
    import urllib.request
    if os.path.exists(dst) and os.path.getsize(dst) == size:
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp, h, got = dst + ".part", hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b); h.update(b); got += len(b)
            progress(lo + (hi - lo) * got / size, f"Downloading {what} ({got >> 20} of {size >> 20} MB, once only)…")
    if h.hexdigest() != sha:
        os.remove(tmp)
        raise RuntimeError(f"The {what} download was damaged. Please try again.")
    os.replace(tmp, dst)
    return dst


def _load_roformer(models_dir, progress):
    with _mlock:
        if "roformer" in _models:
            return _models["roformer"]
    import torch
    from roformer.bs_roformer import BSRoformer
    url, sha, size = ROFORMER["ckpt"]
    p = _download(url, os.path.join(models_dir, "roformer", "vocals_bs_roformer_317.ckpt"), sha, size, progress, 0.0, 0.25, "the vocal model")
    m = BSRoformer(**ROFORMER["model"])
    sd = torch.load(p, map_location="cpu", weights_only=True)
    m.load_state_dict(sd.get("state_dict", sd) if isinstance(sd, dict) else sd)
    m.eval()
    with _mlock:
        _models["roformer"] = m
    return m


_GPU_OK = {}


def _trust_gpu(key, run, dev):
    """Run a short excerpt on the GPU and on the CPU. Use the GPU only if both agree: some PyTorch
    GPU back-ends (notably Apple's MPS) can silently return wrong numbers, which sounds like noise."""
    if dev == "cpu":
        return "cpu"
    if key not in _GPU_OK:
        try:
            a, b = np.asarray(run(dev), np.float64), np.asarray(run("cpu"), np.float64)
            err = float(np.sum((a - b) ** 2) / (np.sum(b ** 2) + 1e-12))
            _GPU_OK[key] = err < 1e-3 and np.isfinite(a).all()
            print(f"[stems] {key} on {dev}: differs from CPU by {10 * np.log10(err + 1e-20):.1f} dB -> "
                  f"{'using ' + dev if _GPU_OK[key] else 'using the CPU (GPU output was wrong)'}", flush=True)
        except Exception as e:
            _GPU_OK[key] = False
            print(f"[stems] {key} on {dev} failed ({e}); using the CPU", flush=True)
    return dev if _GPU_OK[key] else "cpu"


def _excerpt(wav, secs):
    n = wav.shape[-1]
    L = int(secs * SR)
    s0 = max(0, n // 2 - L // 2)
    seg = wav[..., s0:s0 + L]
    return np.pad(seg, [(0, 0)] * (seg.ndim - 1) + [(0, L - seg.shape[-1])]) if seg.shape[-1] < L else seg


def roformer_vocals(m, wav, dev, progress=None, cancelled=None, lo=0.0, hi=1.0):
    """wav (2, n) float32 at 44.1 kHz -> vocals (2, n). Overlapping windows, cross-faded."""
    import torch
    progress = progress or (lambda f, msg: None)
    C, step = ROFORMER["chunk"], ROFORMER["chunk"] // ROFORMER["overlap"]
    fade = C // 10
    win = np.ones(C, np.float32)
    win[:fade] = np.linspace(0, 1, fade); win[-fade:] = np.linspace(1, 0, fade)
    n = wav.shape[1]
    border = C - step
    x = np.pad(wav, ((0, 0), (border, border)), mode="reflect" if n > border else "constant")
    N = x.shape[1]
    starts = list(range(0, max(1, N - border), step))
    out, cnt = np.zeros_like(x), np.zeros(N, np.float32)

    def one(d):
        with torch.no_grad():
            return m.to(d)(torch.from_numpy(np.ascontiguousarray(_excerpt(wav, C / SR)))[None].to(d)).float().cpu().numpy()
    dev = _trust_gpu("vocal model", one, dev)
    mdev = m.to(dev)
    for k, s0 in enumerate(starts):
        if cancelled and cancelled():
            raise InterruptedError
        seg = x[:, s0:s0 + C]
        L = seg.shape[1]
        if L < C:
            seg = np.pad(seg, ((0, 0), (0, C - L)))
        w = win.copy()
        if s0 == 0:
            w[:fade] = 1
        if s0 + C >= N:
            w[-fade:] = 1
        with torch.no_grad():
            y = mdev(torch.from_numpy(np.ascontiguousarray(seg))[None].to(dev))
        y = y.reshape(-1, 2, C)[0].float().cpu().numpy()
        out[:, s0:s0 + L] += (y * w)[:, :L]
        cnt[s0:s0 + L] += w[:L]
        progress(lo + (hi - lo) * (k + 1) / len(starts), "Separating the vocals…")
    out = out / np.maximum(cnt, 1e-6)
    return out[:, border:border + n]


def _demucs(m, mix, dev, progress, cancelled, lo, hi, msg):
    import torch
    from demucs import apply as dapply
    from demucs.apply import apply_model

    class _Bar:  # Demucs reports progress through tqdm: route it to our callback
        def __init__(self, it=None, total=None, **kw):
            self.it = list(it) if it is not None else []
            self.total = total or len(self.it) or 1

        def __iter__(self):
            for k, x in enumerate(self.it):
                if cancelled and cancelled():
                    raise InterruptedError
                progress(lo + (hi - lo) * k / max(1, len(self.it)), msg)
                yield x

    orig = dapply.tqdm.tqdm
    dapply.tqdm.tqdm = _Bar
    try:
        wav = torch.from_numpy(mix)
        ref = wav.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        x = ((wav - mean) / std)[None]
        ex = torch.from_numpy(np.ascontiguousarray(_excerpt(x.numpy(), 10)))

        def one(d):
            with torch.no_grad():
                return apply_model(m, ex.to(d), device=d, shifts=0, split=True, overlap=0.25).cpu().numpy()
        dapply.tqdm.tqdm = orig
        dev = _trust_gpu("stem model " + "/".join(m.sources), one, dev)
        dapply.tqdm.tqdm = _Bar
        try:
            with torch.no_grad():
                out = apply_model(m, x.to(dev), device=dev, shifts=1, split=True, overlap=0.25, progress=True)
        except (RuntimeError, NotImplementedError):          # e.g. an op missing on the GPU: use the CPU
            if dev == "cpu":
                raise
            with torch.no_grad():
                out = apply_model(m, x, device="cpu", shifts=1, split=True, overlap=0.25, progress=True)
        return (out[0] * std + mean).cpu().numpy()
    finally:
        dapply.tqdm.tqdm = orig


def split_stems(path: str, cache_root: str, models_dir: str, model: str = "htdemucs", progress=None, cancelled=None):
    """Separate a song into stems (Demucs, or RoFormer vocals + Demucs). Cached per song + model. Returns {stem: flac path}."""
    import soundfile as sf

    hit = cached_stems(cache_root, path, model)
    if hit:
        return hit
    progress = progress or (lambda f, msg: None)
    dev = _torch_device()
    mix = audio(path).astype(np.float32)
    sources = {}
    if model == "roformer":
        progress(0.0, "Loading the vocal model…")
        rf = _load_roformer(models_dir, progress)
        try:
            voc = roformer_vocals(rf, mix, dev, progress, cancelled, 0.25, 0.7)
        except (RuntimeError, NotImplementedError):
            if dev == "cpu":
                raise
            voc = roformer_vocals(rf, mix, "cpu", progress, cancelled, 0.25, 0.7)
        rest = mix - voc
        progress(0.7, "Loading the drum & bass model…")
        m = _load_model("htdemucs", models_dir)
        out = _demucs(m, rest, dev, progress, cancelled, 0.72, 0.95, "Splitting drums & bass…")
        sources = {"vocals": voc, "drums": out[m.sources.index("drums")], "bass": out[m.sources.index("bass")]}
    else:
        progress(0.0, "Loading the stem model (downloads once, about 80 MB)…")
        m = _load_model(model, models_dir)
        progress(0.02, "Splitting…")
        out = _demucs(m, mix, dev, progress, cancelled, 0.02, 0.95, "Splitting…")
        sources = {name: out[i] for i, name in enumerate(m.sources) if name != "other"}
    # 'other' is everything the named stems didn't take, so all stems together add up to the original exactly
    sources["other"] = mix - sum(sources.values())
    progress(0.96, "Saving stems…")
    d = stems_dir(cache_root, path, model)
    os.makedirs(d, exist_ok=True)
    files = {}
    for name, y in sources.items():
        p = os.path.join(d, name + ".flac")
        sf.write(p + ".part.flac", np.clip(y.T, -1, 1), SR, subtype="PCM_24")
        os.replace(p + ".part.flac", p)
        files[name] = p
    progress(1.0, "Done")
    return {n: files[n] for n in STEM_MODELS[model]["stems"] if n in files}


def render_mix(stems: dict | None, mix_path: str, dst: str, gains: dict, semis: float, ratio: float) -> str:
    """Whole song (or the stems you kept, at their volumes), transposed + tempo-changed, as 24-bit WAV."""
    import soundfile as sf
    if stems:
        acc = None
        for name, p in stems.items():
            g = float(gains.get(name, 1.0))
            if g <= 0.001:
                continue
            a = audio(p) * g
            acc = a if acc is None else acc[:, : a.shape[1]] + a[:, : acc.shape[1]]
        if acc is None:
            raise RuntimeError("All stems are muted")
        a = acc
    else:
        a = audio(mix_path)
    if abs(ratio - 1) > 1e-4 or abs(semis) > 1e-4:
        from pedalboard import time_stretch
        a = time_stretch(np.ascontiguousarray(a, dtype=np.float32), SR, stretch_factor=float(ratio),
                         pitch_shift_in_semitones=float(semis), high_quality=True, transient_mode="crisp",
                         preserve_formants=True)
    tmp = dst + ".part.wav"
    sf.write(tmp, np.clip(a.T, -1, 1), SR, subtype="PCM_24")
    os.replace(tmp, dst)
    return dst
