"""
Tru Studio Caption Remover - core engine.

Pipeline:
  1. (optional) detect text on every Nth frame with EasyOCR's CRAFT detector
  2. build a per-frame mask = auto text boxes + manual boxes (graphics, logos, lower thirds)
  3. fill the masked area: Fast = OpenCV inpaint, Quality = LaMa
  4. temporal stabilising: when the shot is static and the mask hasn't moved,
     reuse/blend the previous fill so the patch doesn't flicker
  5. re-encode with ffmpeg and copy the original audio back in
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass, field

import cv2
import numpy as np

warnings.filterwarnings("ignore")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Packaged builds point these at the app's support folder; dev runs use PATH + ./models
MODEL_DIR = os.environ.get("CR_MODELS") or os.path.join(APP_DIR, "models")
FFMPEG = os.environ.get("CR_FFMPEG") or "ffmpeg"
FFPROBE = os.environ.get("CR_FFPROBE") or "ffprobe"
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console flashes on Windows

_encoders = None


def h264_args():
    """Best available H.264 encoder: libx264 > Apple VideoToolbox > Windows Media Foundation."""
    global _encoders
    if _encoders is None:
        try:
            _encoders = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True,
                                       text=True, **_NOWIN).stdout
        except Exception:
            _encoders = ""
    if " libx264 " in _encoders:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"]
    if "h264_videotoolbox" in _encoders:
        return ["-c:v", "h264_videotoolbox", "-q:v", "70", "-pix_fmt", "yuv420p", "-allow_sw", "1"]
    if "h264_mf" in _encoders:
        return ["-c:v", "h264_mf", "-quality", "100", "-rate_control", "quality", "-pix_fmt", "nv12"]
    return ["-c:v", "mpeg4", "-q:v", "2"]


# --------------------------------------------------------------------------- #
#  Video I/O
# --------------------------------------------------------------------------- #
def probe(path: str) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, check=True, **_NOWIN).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    if not fps or math.isnan(fps):
        fps = 30.0
    dur = float(v.get("duration") or info["format"].get("duration") or 0)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    if abs(rot) in (90, 270):
        w, h = h, w
    nb = v.get("nb_frames")
    frames = int(nb) if nb and nb.isdigit() and int(nb) > 0 else int(round(dur * fps))
    return {"width": w, "height": h, "fps": fps, "duration": dur, "frames": max(frames, 1),
            "has_audio": has_audio, "fps_str": v.get("avg_frame_rate") or f"{fps}"}


def read_frame(path: str, t: float, info: dict) -> np.ndarray:
    """Single RGB frame at time t (seconds)."""
    w, h = info["width"], info["height"]
    cmd = [FFMPEG, "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", path, "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, **_NOWIN).stdout
    if len(raw) < w * h * 3:  # past the end -> step back a little
        if t > 0.2:
            return read_frame(path, t - 0.2, info)
        raise RuntimeError("Could not decode a frame from this video")
    return np.frombuffer(raw[: w * h * 3], np.uint8).reshape(h, w, 3).copy()


class FrameReader:
    """Streams every frame of a video as RGB numpy arrays via ffmpeg."""

    def __init__(self, path, info, every: int = 1, scale_w: int | None = None):
        self.w, self.h = info["width"], info["height"]
        vf = []
        if every > 1:
            vf.append(f"select='not(mod(n\\,{every}))'")
        if scale_w and scale_w < self.w:
            sh = int(round(self.h * scale_w / self.w / 2) * 2)
            vf.append(f"scale={scale_w}:{sh}")
            self.w, self.h = scale_w, sh
        cmd = [FFMPEG, "-v", "error", "-i", path]
        if vf:
            cmd += ["-vf", ",".join(vf)]
            if every > 1:
                cmd += ["-fps_mode", "passthrough"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  bufsize=self.w * self.h * 3 * 4, **_NOWIN)
        self.n = self.w * self.h * 3

    def __iter__(self):
        while True:
            buf = self.p.stdout.read(self.n)
            if len(buf) < self.n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
        self.close()

    def close(self):
        try:
            self.p.stdout.close()
            self.p.kill()
            self.p.wait(timeout=5)
        except Exception:
            pass


class FrameWriter:
    def __init__(self, src, out, info, fmt="mp4"):
        w, h = info["width"], info["height"]
        cmd = [FFMPEG, "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", info["fps_str"], "-i", "-",
               "-i", src, "-map", "0:v:0", "-map", "1:a?"]
        if fmt == "prores":
            cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
                    "-c:a", "pcm_s16le"]
        else:
            cmd += h264_args() + ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]
        cmd += ["-shortest", out]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_NOWIN)

    def write(self, rgb: np.ndarray):
        self.p.stdin.write(np.ascontiguousarray(rgb).tobytes())

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="ignore")
        self.p.wait()
        if self.p.returncode != 0:
            raise RuntimeError("ffmpeg encode failed: " + err[-600:])

    def kill(self):
        try:
            self.p.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Models (loaded lazily, shared)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_reader = None
_lama = None
_device = None


def device():
    global _device
    if _device is None:
        import torch
        if torch.cuda.is_available():
            _device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            _device = "mps"
        else:
            _device = "cpu"
    return _device


def text_reader():
    global _reader
    with _lock:
        if _reader is None:
            import easyocr
            gpu = device() if device() != "cpu" else False
            try:
                _reader = easyocr.Reader(["en"], gpu=gpu, model_storage_directory=MODEL_DIR,
                                         download_enabled=False, recognizer=False, verbose=False)
            except Exception:
                _reader = easyocr.Reader(["en"], gpu=False, model_storage_directory=MODEL_DIR,
                                         download_enabled=False, recognizer=False, verbose=False)
    return _reader


def lama():
    global _lama, _device
    with _lock:
        if _lama is None:
            import torch
            m = torch.jit.load(os.path.join(MODEL_DIR, "big-lama.pt"), map_location="cpu").eval()
            dev = device()
            if dev != "cpu":
                try:
                    m = m.to(dev)
                    with torch.no_grad():  # warm-up / capability check
                        m(torch.rand(1, 3, 64, 64, device=dev), torch.zeros(1, 1, 64, 64, device=dev))
                except Exception:
                    m = m.to("cpu")
                    _device = "cpu"
            _lama = m
    return _lama


# --------------------------------------------------------------------------- #
#  Detection  (CRAFT via EasyOCR: word boxes + per-pixel character heatmap)
# --------------------------------------------------------------------------- #
DETECT_W = 1280
HEAT_T = 0.20          # heatmap level counted as "letter"


def _craft_maps(rgb):
    import torch
    from easyocr.imgproc import normalizeMeanVariance, resize_aspect_ratio
    rd = text_reader()
    img, ratio, _ = resize_aspect_ratio(rgb, DETECT_W, interpolation=cv2.INTER_LINEAR, mag_ratio=1.0)
    x = torch.from_numpy(normalizeMeanVariance(img).transpose(2, 0, 1)).unsqueeze(0).to(rd.device)
    with torch.no_grad():
        y, _ = rd.detector(x)
    y = y[0].float().cpu().numpy()
    return y[:, :, 0], y[:, :, 1], ratio


def detect_hits(rgb: np.ndarray, sensitivity: float = 0.5) -> list[dict]:
    """Text found in a frame: [{"box": [x0,y0,x1,y1] normalised, "heat": uint8 letter map of the box}]"""
    from easyocr.craft_utils import adjustResultCoordinates, getDetBoxes
    h, w = rgb.shape[:2]
    st, sl, ratio = _craft_maps(rgb)
    thr = 0.8 - 0.4 * sensitivity            # sensitivity 0..1 -> text_threshold 0.8..0.4
    boxes, _, _ = getDetBoxes(st, sl, thr, 0.3, max(0.2, thr - 0.3), False, False)
    boxes = adjustResultCoordinates(boxes, 1 / ratio, 1 / ratio)
    hs = ratio / 2.0                          # heatmap is half the detector resolution
    hits = []
    for b in boxes:
        b = np.asarray(b, np.float32)
        x0, y0 = b.min(0)
        x1, y1 = b.max(0)
        m = 0.15 * (y1 - y0) + 2              # room for outlines / shadows
        x0, y0 = max(0.0, x0 - m), max(0.0, y0 - m)
        x1, y1 = min(float(w), x1 + m), min(float(h), y1 + m)
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        hx0, hy0 = int(x0 * hs), int(y0 * hs)
        hx1, hy1 = max(hx0 + 1, int(math.ceil(x1 * hs))), max(hy0 + 1, int(math.ceil(y1 * hs)))
        heat = (np.clip(st[hy0:hy1, hx0:hx1], 0, 1) * 255).astype(np.uint8)
        hits.append({"box": [x0 / w, y0 / h, x1 / w, y1 / h], "heat": heat})
    return hits


def detect_text(rgb: np.ndarray, sensitivity: float = 0.5) -> list[list[float]]:
    """Text boxes as normalised [x0, y0, x1, y1] (for the interface)."""
    return [[float(v) for v in hit["box"]] for hit in detect_hits(rgb, sensitivity)]


def in_zone(box, zone):
    if not zone:
        return True
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return zone[0] <= cx <= zone[2] and zone[1] <= cy <= zone[3]


# --------------------------------------------------------------------------- #
#  Masks
# --------------------------------------------------------------------------- #
def _px(b, w, h, pad=0):
    return (max(int(b[0] * w) - pad, 0), max(int(b[1] * h) - pad, 0),
            min(int(math.ceil(b[2] * w)) + pad, w), min(int(math.ceil(b[3] * h)) + pad, h))


def boxes_to_mask(boxes, w, h, pad_px):
    m = np.zeros((h, w), np.uint8)
    for b in boxes:
        x0, y0, x1, y1 = _px(b, w, h, pad_px)
        m[y0:y1, x0:x1] = 255
    return m


def _plate(rgb, x0, y0, x1, y1, letters, d):
    """If the letters sit on a solid caption plate (TikTok/Reels style boxes), return the plate's
    mask in frame coords for the region (x0,y0,x1,y1); else None. A plain uniform background that
    simply continues past the text (sky, wall) is not a plate: plates end inside the region."""
    reg = rgb[y0:y1, x0:x1].astype(np.int16)
    rh, rw = letters.shape
    lt = letters.astype(np.uint8)
    k1 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * d + 1, 2 * d + 1))
    k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * d + 7, 2 * d + 7))
    ring = (cv2.dilate(lt, k2) > 0) & ~(cv2.dilate(lt, k1) > 0)
    if ring.sum() < 40:
        return None
    px = reg[ring]
    col = np.median(px, 0)
    dist_ring = np.abs(px - col).max(1)
    if (dist_ring < 26).mean() < 0.72 or px.std(0).max() > 18:
        return None                                    # varied surroundings: no plate
    near = (np.abs(reg - col).max(2) < 30).astype(np.uint8)
    n, lab = cv2.connectedComponents(near, 4)
    keep = np.unique(lab[ring & (near > 0)])
    keep = keep[keep > 0]
    if not len(keep):
        return None
    plate = np.isin(lab, keep)
    # a plate is closed top and bottom inside the region; background runs off the edges
    if plate[0].mean() > 0.3 or plate[-1].mean() > 0.3 or (plate[:, 0].mean() > 0.6 and plate[:, -1].mean() > 0.6):
        return None
    plate = cv2.morphologyEx(plate.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    # fill the plate solid (covers letters inside it that the detector missed)
    cnts, _ = cv2.findContours(plate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(plate, cnts, -1, 255, -1)
    return plate


def hits_to_mask(hits, w, h, pad_px, precise=True, zone=None, rgb=None):
    """Letter-shaped mask (precise) or full boxes, for the text hits that fall in the zone.
    With the frame given, solid caption plates behind the letters are included too."""
    m = np.zeros((h, w), np.uint8)
    for hit in hits:
        if not in_zone(hit["box"], zone):
            continue
        x0, y0, x1, y1 = _px(hit["box"], w, h)
        bw, bh = x1 - x0, y1 - y0
        if bw < 2 or bh < 2:
            continue
        heat = hit.get("heat")
        if not precise or heat is None or heat.size == 0:
            xa, ya, xb, yb = _px(hit["box"], w, h, pad_px)
            m[ya:yb, xa:xb] = 255
            continue
        g = cv2.resize(heat, (bw, bh), interpolation=cv2.INTER_LINEAR) > int(HEAT_T * 255)
        if g.mean() < 0.04:                     # detector unsure of letter shapes -> whole box
            xa, ya, xb, yb = _px(hit["box"], w, h, pad_px)
            m[ya:yb, xa:xb] = 255
            continue
        d = int(round(0.10 * bh)) + pad_px      # grow over outline, shadow and anti-aliasing
        canvas = np.zeros((bh + 2 * d, bw + 2 * d), np.uint8)
        canvas[d:d + bh, d:d + bw] = g * 255
        if d > 0:
            canvas = cv2.dilate(canvas, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * d + 1, 2 * d + 1)))
        cx0, cy0 = x0 - d, y0 - d
        sx0, sy0 = max(0, -cx0), max(0, -cy0)
        tx0, ty0 = max(0, cx0), max(0, cy0)
        tx1, ty1 = min(w, cx0 + canvas.shape[1]), min(h, cy0 + canvas.shape[0])
        m[ty0:ty1, tx0:tx1] |= canvas[sy0:sy0 + ty1 - ty0, sx0:sx0 + tx1 - tx0]
        if rgb is not None:                       # caption plate behind the letters?
            e = int(0.9 * bh) + d
            ex = max(e, 4 * bh)                    # plates often run past the detected words
            rx0, ry0, rx1, ry1 = max(0, x0 - ex), max(0, y0 - e), min(w, x1 + ex), min(h, y1 + e)
            letters = np.zeros((ry1 - ry0, rx1 - rx0), bool)
            letters[y0 - ry0:y0 - ry0 + bh, x0 - rx0:x0 - rx0 + bw] = g
            pl = _plate(rgb, rx0, ry0, rx1, ry1, letters, max(2, int(round(0.10 * bh))))
            if pl is not None:
                gp = max(2, pad_px)
                pl = cv2.dilate(pl, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * gp + 1, 2 * gp + 1)))
                m[ry0:ry1, rx0:rx1] |= pl
    return m


# --------------------------------------------------------------------------- #
#  Filling
# --------------------------------------------------------------------------- #
def _lama_max_px():
    return 2_400_000 if (_device or device()) != "cpu" else 1_000_000


def _clusters(mask, margin, rel=1.0):
    """Bounding rects around connected mask areas (merged when they overlap)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    rects = []
    h, w = mask.shape
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        m = max(margin, int(rel * max(bh, 24)))
        rects.append([max(x - m, 0), max(y - m, 0), min(x + bw + m, w), min(y + bh + m, h)])
    merged = True
    while merged:
        merged = False
        out = []
        for r in rects:
            for o in out:
                if r[0] < o[2] and r[2] > o[0] and r[1] < o[3] and r[3] > o[1]:
                    o[:] = [min(r[0], o[0]), min(r[1], o[1]), max(r[2], o[2]), max(r[3], o[3])]
                    merged = True
                    break
            else:
                out.append(list(r))
        rects = out
    return rects


def fill_fast(rgb, mask):
    out = rgb.copy()
    for x0, y0, x1, y1 in _clusters(mask, 24, 0.6):
        crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        r = cv2.inpaint(bgr, mask[y0:y1, x0:x1], 5, cv2.INPAINT_TELEA)
        out[y0:y1, x0:x1] = cv2.cvtColor(r, cv2.COLOR_BGR2RGB)
    return out


def fill_quality(rgb, mask):
    import torch
    model = lama()
    dev = torch.device(_device or device())
    out = rgb.copy()
    maxpx = _lama_max_px()
    for x0, y0, x1, y1 in _clusters(mask, 64, 1.2):     # generous context around each hole
        crop = rgb[y0:y1, x0:x1]
        mc = mask[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        s = min(1.0, math.sqrt(maxpx / float(ch * cw)))  # full resolution unless very large
        tw, th = max(8, int(cw * s)), max(8, int(ch * s))
        pw, ph = (8 - tw % 8) % 8, (8 - th % 8) % 8
        ci = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA) if s < 1 else crop
        mi = cv2.resize(mc, (tw, th), interpolation=cv2.INTER_NEAREST) if s < 1 else mc
        ci = np.pad(ci, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        mi = np.pad(mi, ((0, ph), (0, pw)), mode="constant")
        it = torch.from_numpy(np.ascontiguousarray(ci)).permute(2, 0, 1)[None].float().div(255).to(dev)
        mt = torch.from_numpy((mi > 127).astype(np.float32))[None, None].to(dev)
        with torch.no_grad():
            r = model(it, mt)[0].permute(1, 2, 0).clamp(0, 1).mul(255).byte().cpu().numpy()
        r = r[:th, :tw]
        if s < 1:
            r = cv2.resize(r, (cw, ch), interpolation=cv2.INTER_CUBIC)
        sel = mc > 0
        out[y0:y1, x0:x1][sel] = r[sel]
    return out


def composite(rgb, filled, mask, feather=1.5):
    a = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), feather)[..., None]
    a = np.maximum(a, (mask[..., None] > 0) * 1.0)
    return (filled.astype(np.float32) * a + rgb.astype(np.float32) * (1 - a)).astype(np.uint8)


def process_frame(rgb, mask, mode):
    if not mask.any():
        return rgb
    filled = fill_quality(rgb, mask) if mode == "quality" else fill_fast(rgb, mask)
    return composite(rgb, filled, mask)


# --------------------------------------------------------------------------- #
#  Temporal propagation: copy the real background from nearby frames
# --------------------------------------------------------------------------- #
FLOW_W = 640
OFFSETS = [1, -1, 2, -2, 4, -4, 8, -8, 16, -16, 24, -24, 36, -36, 48, -48]
_dis_local = threading.local()


def _dis():
    d = getattr(_dis_local, "d", None)
    if d is None:
        d = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        _dis_local.d = d
    return d


def small_gray(rgb):
    h, w = rgb.shape[:2]
    s = min(1.0, FLOW_W / w)
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return cv2.resize(g, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else g


class Propagator:
    """For frame i, look through neighbouring frames (closest first). Where the camera or the text
    moved, the background hidden under the text in frame i is visible in frame j: estimate how the
    picture moved around each hole (from optical flow on the surrounding ring), shift frame j into
    place, check it lines up, and copy the uncovered pixels. Whatever stays hidden is left for LaMa."""

    def __init__(self, offsets=OFFSETS):
        self.offsets = offsets

    def run(self, i, frames, masks, grays):
        fr = frames[i]
        m = masks[i] > 0
        out = fr.copy()
        rem = m.copy()
        H, W = m.shape
        g_i = grays[i]
        sc = g_i.shape[1] / W
        regs = self._regions(m)
        flows = {}
        for j in self._candidates(i, m, masks):
            if not rem.any():
                break
            mj = masks[j] > 0
            fj = frames[j]
            for reg in regs:
                x0, y0, x1, y1 = reg["box"]
                r_rem = rem[y0:y1, x0:x1]
                if not r_rem.any():
                    continue
                ring = reg["ring"]
                tf = self._motion(i, j, reg, g_i, grays[j], sc, flows)
                if tf is None:
                    continue
                warped, wmask, inb = self._warp(fj, mj, reg["box"], tf)
                # does frame j line up with frame i around the hole?
                cmp = ring & ~wmask & inb
                if cmp.sum() < 30:
                    continue
                a = fr[y0:y1, x0:x1][cmp].astype(np.float32)
                b = warped[cmp].astype(np.float32)
                err = np.abs(a - b).mean()
                if err > 9.0:
                    continue
                gain = (a.mean(0) + 1) / (b.mean(0) + 1)          # match exposure changes
                take = r_rem & ~wmask & inb
                if not take.any():
                    continue
                src = np.clip(warped.astype(np.float32) * gain, 0, 255).astype(np.uint8)
                out[y0:y1, x0:x1][take] = src[take]
                r_rem[take] = False
        return out, rem

    def _candidates(self, i, m, masks, extra=4):
        """Nearby frames first (best for moving shots), then the frames where most of this hole is
        uncovered (e.g. the gap between two subtitle lines on a still shot)."""
        order = [i + o for o in self.offsets if (i + o) in masks]
        small = lambda a: cv2.resize((a > 0).astype(np.uint8), (max(1, a.shape[1] // 8), max(1, a.shape[0] // 8)),
                                     interpolation=cv2.INTER_NEAREST) > 0
        ms = small(m)
        gains = []
        for j, mj in masks.items():
            if j == i or j in order:
                continue
            g = int((ms & ~small(mj)).sum())
            if g > 0:
                gains.append((-g, abs(j - i), j))
        gains.sort()
        return order + [j for _, _, j in gains[:extra]]

    @staticmethod
    def _regions(m):
        regs = []
        n, lab, stats, _ = cv2.connectedComponentsWithStats(
            cv2.dilate(m.astype(np.uint8), np.ones((9, 9), np.uint8)), 8)
        H, W = m.shape
        for k in range(1, n):
            x, y, bw, bh, _ = stats[k]
            r = max(14, int(0.6 * bh))
            x0, y0, x1, y1 = max(0, x - r), max(0, y - r), min(W, x + bw + r), min(H, y + bh + r)
            sub = m[y0:y1, x0:x1].astype(np.uint8)
            inner = cv2.dilate(sub, np.ones((7, 7), np.uint8)) > 0
            outer = cv2.dilate(sub, np.ones((2 * r + 1, 2 * r + 1), np.uint8)) > 0
            regs.append({"box": (x0, y0, x1, y1), "ring": outer & ~inner})
        return regs

    @staticmethod
    def _motion(i, j, reg, g_i, g_j, sc, flows):
        """2x3 affine mapping frame-i coords -> frame-j coords around this region, or None."""
        x0, y0, x1, y1 = reg["box"]
        sx0, sy0, sx1, sy1 = int(x0 * sc), int(y0 * sc), max(int(x0 * sc) + 1, int(x1 * sc)), max(int(y0 * sc) + 1, int(y1 * sc))
        ring_s = cv2.resize(reg["ring"].astype(np.uint8), (sx1 - sx0, sy1 - sy0), interpolation=cv2.INTER_NEAREST) > 0
        if ring_s.sum() < 20:
            return None
        a = g_i[sy0:sy1, sx0:sx1][ring_s].astype(np.int16)
        b = g_j[sy0:sy1, sx0:sx1][ring_s].astype(np.int16)
        if np.abs(a - b).mean() < 2.5:                 # nothing moved here: same place
            return np.array([[1, 0, 0], [0, 1, 0]], np.float32)
        if j not in flows:
            flows[j] = _dis().calc(g_i, g_j, None)
        fl = flows[j][sy0:sy1, sx0:sx1]
        ys, xs = np.nonzero(ring_s)
        if len(xs) > 1500:
            sel = np.random.default_rng(0).choice(len(xs), 1500, replace=False)
            ys, xs = ys[sel], xs[sel]
        p_i = np.stack([(xs + sx0) / sc, (ys + sy0) / sc], 1).astype(np.float32)
        p_j = p_i + np.stack([fl[ys, xs, 0], fl[ys, xs, 1]], 1) / sc
        M, inl = cv2.estimateAffinePartial2D(p_i, p_j, method=cv2.RANSAC, ransacReprojThreshold=2.0 / sc)
        if M is None or inl is None or inl.mean() < 0.5:
            return None
        return M.astype(np.float32)

    @staticmethod
    def _warp(fj, mj, box, M):
        x0, y0, x1, y1 = box
        H, W = mj.shape
        xs, ys = np.meshgrid(np.arange(x0, x1, dtype=np.float32), np.arange(y0, y1, dtype=np.float32))
        mx = M[0, 0] * xs + M[0, 1] * ys + M[0, 2]
        my = M[1, 0] * xs + M[1, 1] * ys + M[1, 2]
        inb = (mx >= 0) & (mx <= W - 1) & (my >= 0) & (my <= H - 1)
        warped = cv2.remap(fj, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        wm = cv2.remap(mj.astype(np.uint8) * 255, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        return warped, wm > 0, inb


# --------------------------------------------------------------------------- #
#  Settings + per-frame pipeline
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    auto_text: bool = True
    zone: list | None = None            # normalised rect limiting auto text detection
    regions: list = field(default_factory=list)  # [{"box":[x0,y0,x1,y1], "start":s|None, "end":s|None}]
    mode: str = "quality"               # "fast" | "quality"
    padding: int = 6                    # px around text (at 1080p, scaled)
    sensitivity: float = 0.5
    detect_every: int = 3               # run detector every N frames
    stabilise: bool = True
    precise: bool = True                # letter-shaped masks instead of whole boxes
    temporal: bool = True               # borrow real background from nearby frames
    fmt: str = "mp4"                    # "mp4" | "prores"
    start: float | None = None          # optional trim (seconds)
    end: float | None = None

    @classmethod
    def from_dict(cls, d):
        k = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**k)


def scaled_pad(info, pad):
    return max(1, int(round(pad * max(info["width"], info["height"]) / 1920)))


def frame_mask(info, s: Settings, t: float, hits, pad, rgb=None):
    w, h = info["width"], info["height"]
    m = hits_to_mask(hits, w, h, pad, s.precise, s.zone, rgb) if (s.auto_text and hits) else np.zeros((h, w), np.uint8)
    manual = [r["box"] for r in s.regions
              if (r.get("start") is None or t >= r["start"] - 1e-3)
              and (r.get("end") is None or t <= r["end"] + 1e-3)]
    if manual:
        m |= boxes_to_mask(manual, w, h, max(2, pad // 2))
    return m


class FrameFiller:
    """Turns frame i (plus its neighbours) into the cleaned frame. Keeps state for steady fills."""

    def __init__(self, s: Settings):
        self.s = s
        self.prop = Propagator()
        self.prev = None   # (rgb, out, lama_mask)

    def fill(self, i, frames, masks, grays):
        fr, m = frames[i], masks[i]
        if not m.any():
            self.prev = None
            return fr
        if self.s.temporal and len(frames) > 1:
            base, rem = self.prop.run(i, frames, masks, grays)
        else:
            base, rem = fr.copy(), m > 0
        rem_u8 = rem.astype(np.uint8) * 255
        out = base
        if rem.any():
            out = None
            p = self.prev
            if self.s.stabilise and p is not None:
                ring = (cv2.dilate(m, np.ones((25, 25), np.uint8)) > 0) & ~(m > 0)
                if ring.any():
                    diff = np.abs(fr[ring].astype(np.int16) - p[0][ring].astype(np.int16)).mean()
                    if diff < 2.5 and not (rem & ~(p[2] > 0)).any():
                        out = base.copy()
                        out[rem] = p[1][rem]            # still shot: reuse the earlier fill, no flicker
            if out is None:
                filled = fill_quality(base, rem_u8) if self.s.mode == "quality" else fill_fast(base, rem_u8)
                out = filled
        res = composite(fr, out, m)
        self.prev = (fr, res, rem_u8)
        return res


def _hits_for(i, every, keyhits):
    k0 = (i // every) * every
    return keyhits.get(k0, []) + keyhits.get(k0 + every, [])   # union of keyframes on each side


def preview(path, info, s: Settings, t: float):
    """Render one frame exactly as the export would, using a few neighbouring frames."""
    fps = info["fps"]
    i0 = int(round(t * fps))
    pad = scaled_pad(info, s.padding)
    span = int(round(1.6 * fps))
    offs = [0] + (sorted({o for k in (1, 3, 6, 12, 18, 24, 30, 36, 42, span) for o in (k, -k) if abs(k) <= span},
                         key=abs) if s.temporal else [])
    frames, masks, grays = {}, {}, {}
    for o in offs:
        j = i0 + o
        tj = j / fps
        if tj < 0 or tj > info["duration"] - 0.5 / fps:
            continue
        try:
            fr = read_frame(path, tj, info)
        except Exception:
            continue
        hits = detect_hits(fr, s.sensitivity) if s.auto_text else []
        frames[j], masks[j], grays[j] = fr, frame_mask(info, s, tj, hits, pad, fr), small_gray(fr)
    if i0 not in frames:
        fr = read_frame(path, t, info)
        return fr, fr, np.zeros(fr.shape[:2], np.uint8)
    out = FrameFiller(s).fill(i0, frames, masks, grays)
    return frames[i0], out, masks[i0]


# --------------------------------------------------------------------------- #
#  Job
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, path, out, info, settings: Settings):
        self.path, self.out, self.info, self.s = path, out, info, settings
        self.state = "queued"
        self.stage = ""
        self.progress = 0.0
        self.eta = None
        self.error = None
        self.cancelled = False
        self.t0 = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled = True

    def status(self):
        return {"state": self.state, "stage": self.stage, "progress": round(self.progress, 4),
                "eta": self.eta, "error": self.error, "device": _device}

    # ----------------------------------------------------------------------
    def _run(self):
        try:
            self.t0 = time.time()
            self.state = "running"
            src = self.path
            info = self.info
            if self.s.start is not None or self.s.end is not None:
                self.stage = "Trimming"
                src = self._trim()
                info = probe(src)
            N = max(1, info["frames"])
            every = max(1, int(self.s.detect_every))

            # ---- pass 1: find text on keyframes --------------------------------
            keyhits = {}
            if self.s.auto_text:
                self.stage = "Finding text"
                text_reader()
                rd = FrameReader(src, info, every=every)
                total_k = max(1, math.ceil(N / every))
                for k, fr in enumerate(rd):
                    if self.cancelled:
                        rd.close()
                        raise InterruptedError
                    keyhits[k * every] = detect_hits(fr, self.s.sensitivity)
                    self.progress = 0.25 * (k + 1) / total_k
                    self._eta()

            # ---- pass 2: clean every frame -------------------------------------
            self.stage = "Removing" + (" (Quality)" if self.s.mode == "quality" else " (Fast)")
            if self.s.mode == "quality":
                lama()
            pad = scaled_pad(info, self.s.padding)
            K = int(min(60, max(max(abs(o) for o in OFFSETS), round(2 * info["fps"])))) if self.s.temporal else 0
            if info["width"] * info["height"] > 2560 * 1440:
                K = min(K, 24)                      # 4K: smaller frame window to save memory
            filler = FrameFiller(self.s)
            if K < max(abs(o) for o in OFFSETS):
                filler.prop.offsets = [o for o in OFFSETS if abs(o) <= K]
            fps = info["fps"]
            base = 0.25 if self.s.auto_text else 0.0
            frames, masks, grays = {}, {}, {}
            it = iter(FrameReader(src, info))
            loaded, eof = 0, False

            def load_until(n):
                nonlocal loaded, eof
                while not eof and loaded <= n:
                    try:
                        fr = next(it)
                    except StopIteration:
                        eof = True
                        break
                    t = loaded / fps
                    mk = frame_mask(info, self.s, t, _hits_for(loaded, every, keyhits), pad, fr)
                    frames[loaded], masks[loaded] = fr, mk
                    grays[loaded] = small_gray(fr) if self.s.temporal else None
                    loaded += 1

            wr = FrameWriter(src, self.out, info, self.s.fmt)
            try:
                i = 0
                while True:
                    if self.cancelled:
                        raise InterruptedError
                    load_until(i + K)
                    if i >= loaded:
                        break
                    wr.write(filler.fill(i, frames, masks, grays))
                    for d in [x for x in frames if x < i - K]:
                        frames.pop(d, None); masks.pop(d, None); grays.pop(d, None)
                    i += 1
                    self.progress = base + (1 - base) * min(1.0, i / N)
                    self._eta()
                wr.close()
            except BaseException:
                wr.kill()
                raise
            self.stage = "Done"
            self.progress = 1.0
            self.eta = 0
            self.state = "done"
        except InterruptedError:
            self.state = "cancelled"
            self.stage = "Cancelled"
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            self.state = "error"
            self.error = str(e)
        finally:
            tmp = self.out + ".trim.mov"
            if os.path.exists(tmp):
                os.remove(tmp)
            if self.state != "done" and os.path.exists(self.out):
                try:
                    os.remove(self.out)
                except OSError:
                    pass

    def _trim(self):
        tmp = self.out + ".trim.mov"
        cmd = [FFMPEG, "-v", "error", "-y"]
        if self.s.start:
            cmd += ["-ss", f"{self.s.start:.3f}"]
        cmd += ["-i", self.path]
        if self.s.end:
            cmd += ["-t", f"{self.s.end - (self.s.start or 0):.3f}"]
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-c:a", "pcm_s16le", tmp]
        subprocess.run(cmd, check=True, capture_output=True, **_NOWIN)
        return tmp

    def _eta(self):
        el = time.time() - self.t0
        if self.progress > 0.02:
            self.eta = int(el / self.progress - el)
