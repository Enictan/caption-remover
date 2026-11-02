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
#  Detection
# --------------------------------------------------------------------------- #
DETECT_W = 1280


def detect_text(rgb: np.ndarray, sensitivity: float = 0.5) -> list[list[float]]:
    """Return text boxes as normalised [x0, y0, x1, y1]."""
    h, w = rgb.shape[:2]
    s = min(1.0, DETECT_W / max(w, h))
    small = cv2.resize(rgb, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else rgb
    sh, sw = small.shape[:2]
    thr = 0.8 - 0.4 * sensitivity           # sensitivity 0..1 -> text_threshold 0.8..0.4
    hz, free = text_reader().detect(small, text_threshold=thr, low_text=max(0.2, thr - 0.3),
                                    link_threshold=0.3, canvas_size=DETECT_W, mag_ratio=1.0,
                                    width_ths=0.8, add_margin=0.08, min_size=8)
    boxes = []
    for x0, x1, y0, y1 in hz[0]:
        boxes.append([x0 / sw, y0 / sh, x1 / sw, y1 / sh])
    for poly in free[0]:
        p = np.array(poly)
        boxes.append([p[:, 0].min() / sw, p[:, 1].min() / sh, p[:, 0].max() / sw, p[:, 1].max() / sh])
    return [[float(np.clip(v, 0, 1)) for v in b] for b in boxes]


def in_zone(box, zone):
    if not zone:
        return True
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return zone[0] <= cx <= zone[2] and zone[1] <= cy <= zone[3]


# --------------------------------------------------------------------------- #
#  Masks
# --------------------------------------------------------------------------- #
def boxes_to_mask(boxes, w, h, pad_px):
    m = np.zeros((h, w), np.uint8)
    for b in boxes:
        x0, y0, x1, y1 = int(b[0] * w) - pad_px, int(b[1] * h) - pad_px, \
            int(math.ceil(b[2] * w)) + pad_px, int(math.ceil(b[3] * h)) + pad_px
        cv2.rectangle(m, (max(x0, 0), max(y0, 0)), (min(x1, w - 1), min(y1, h - 1)), 255, -1)
    return m


# --------------------------------------------------------------------------- #
#  Filling
# --------------------------------------------------------------------------- #
LAMA_MAX = 1024


def _clusters(mask, margin):
    """Bounding rects around connected mask areas (merged when they overlap)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    rects = []
    h, w = mask.shape
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        m = max(margin, int(0.6 * max(bh, 24)))
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
    for x0, y0, x1, y1 in _clusters(mask, 24):
        crop = np.ascontiguousarray(rgb[y0:y1, x0:x1])
        mc = mask[y0:y1, x0:x1]
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        r = cv2.inpaint(bgr, mc, 7, cv2.INPAINT_TELEA)
        out[y0:y1, x0:x1] = cv2.cvtColor(r, cv2.COLOR_BGR2RGB)
    return out


def fill_quality(rgb, mask):
    import torch
    model = lama()
    dev = torch.device(_device or device())
    out = rgb.copy()
    for x0, y0, x1, y1 in _clusters(mask, 48):
        crop = rgb[y0:y1, x0:x1]
        mc = mask[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        s = min(1.0, LAMA_MAX / max(ch, cw))
        tw, th = max(8, int(cw * s)), max(8, int(ch * s))
        pw, ph = (8 - tw % 8) % 8, (8 - th % 8) % 8
        ci = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA) if s < 1 else crop
        mi = cv2.resize(mc, (tw, th), interpolation=cv2.INTER_NEAREST) if s < 1 else mc
        ci = np.pad(ci, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        mi = np.pad(mi, ((0, ph), (0, pw)), mode="constant")
        it = torch.from_numpy(ci).permute(2, 0, 1)[None].float().div(255).to(dev)
        mt = torch.from_numpy((mi > 127).astype(np.float32))[None, None].to(dev)
        with torch.no_grad():
            r = model(it, mt)[0].permute(1, 2, 0).clamp(0, 1).mul(255).byte().cpu().numpy()
        r = r[:th, :tw]
        if s < 1:
            r = cv2.resize(r, (cw, ch), interpolation=cv2.INTER_CUBIC)
        out[y0:y1, x0:x1] = r
    return out


def composite(rgb, filled, mask, feather=3):
    a = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), feather)[..., None]
    a = np.maximum(a, (mask[..., None] > 0) * 1.0)
    return (filled * a + rgb * (1 - a)).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  Job
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    auto_text: bool = True
    zone: list | None = None            # normalised rect limiting auto text detection
    regions: list = field(default_factory=list)  # [{"box":[x0,y0,x1,y1], "start":s|None, "end":s|None}]
    mode: str = "quality"               # "fast" | "quality"
    padding: int = 6                    # px around boxes (at source res, scaled for 1080p)
    sensitivity: float = 0.5
    detect_every: int = 3               # run detector every N frames
    stabilise: bool = True
    fmt: str = "mp4"                    # "mp4" | "prores"
    start: float | None = None          # optional trim (seconds)
    end: float | None = None

    @classmethod
    def from_dict(cls, d):
        k = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**k)


def frame_mask(info, s: Settings, t: float, text_boxes, pad):
    w, h = info["width"], info["height"]
    boxes = [b for b in text_boxes if in_zone(b, s.zone)] if s.auto_text else []
    manual = [r["box"] for r in s.regions
              if (r.get("start") is None or t >= r["start"] - 1e-3)
              and (r.get("end") is None or t <= r["end"] + 1e-3)]
    m = boxes_to_mask(boxes, w, h, pad)
    if manual:
        m |= boxes_to_mask(manual, w, h, max(2, pad // 2))
    return m


def scaled_pad(info, pad):
    return max(1, int(round(pad * max(info["width"], info["height"]) / 1920)))


def process_frame(rgb, mask, mode):
    if not mask.any():
        return rgb
    filled = fill_quality(rgb, mask) if mode == "quality" else fill_fast(rgb, mask)
    return composite(rgb, filled, mask)


def preview(path, info, s: Settings, t: float):
    rgb = read_frame(path, t, info)
    boxes = detect_text(rgb, s.sensitivity) if s.auto_text else []
    pad = scaled_pad(info, s.padding)
    m = frame_mask(info, s, t, boxes, pad)
    return rgb, process_frame(rgb, m, s.mode), m


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
            N = info["frames"]
            every = max(1, int(self.s.detect_every))

            # ---- pass 1: detection on keyframes ------------------------------
            keyboxes = {}
            if self.s.auto_text:
                self.stage = "Finding text"
                text_reader()
                rd = FrameReader(src, info, every=every)
                total_k = max(1, math.ceil(N / every))
                for k, fr in enumerate(rd):
                    if self.cancelled:
                        rd.close()
                        raise InterruptedError
                    keyboxes[k * every] = detect_text(fr, self.s.sensitivity)
                    self.progress = 0.25 * (k + 1) / total_k
                    self._eta()

            # ---- pass 2: fill ------------------------------------------------
            self.stage = "Removing" + (" (Quality)" if self.s.mode == "quality" else " (Fast)")
            if self.s.mode == "quality":
                lama()
            pad = scaled_pad(info, self.s.padding)
            keys = sorted(keyboxes)
            wr = FrameWriter(src, self.out, info, self.s.fmt)
            prev_rgb = prev_out = prev_mask = None
            base = 0.25 if self.s.auto_text else 0.0
            try:
                for i, fr in enumerate(FrameReader(src, info)):
                    if self.cancelled:
                        raise InterruptedError
                    t = i / info["fps"]
                    tb = []
                    if keys:
                        # union of nearest keyframe on each side -> covers word-by-word pop-ons
                        k0 = (i // every) * every
                        for k in (k0, k0 + every):
                            tb += keyboxes.get(k, [])
                    m = frame_mask(info, self.s, t, tb, pad)
                    out = self._fill_stable(fr, m, prev_rgb, prev_out, prev_mask)
                    wr.write(out)
                    prev_rgb, prev_out, prev_mask = fr, out, m
                    self.progress = base + (1 - base) * (i + 1) / N
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

    def _fill_stable(self, fr, m, prev_rgb, prev_out, prev_mask):
        if not m.any():
            return fr
        if self.s.stabilise and prev_out is not None and prev_mask is not None:
            ring = cv2.dilate(m, np.ones((25, 25), np.uint8)) & ~m
            sel = ring > 0
            if sel.any():
                diff = np.abs(fr[sel].astype(np.int16) - prev_rgb[sel].astype(np.int16)).mean()
                same = (m > 0) & (prev_mask > 0)
                new_px = (m > 0) & ~(prev_mask > 0)
                if diff < 2.5 and not new_px.any():
                    # static shot, mask unchanged/shrunk -> reuse previous fill exactly (no flicker)
                    out = fr.copy()
                    out[m > 0] = prev_out[m > 0]
                    return composite(fr, out, m)
                res = process_frame(fr, m, self.s.mode)
                if diff < 6 and same.any():
                    # gently moving shot -> blend toward previous fill to calm flicker
                    a = 0.5 * (1 - diff / 6)
                    res = res.astype(np.float32)
                    res[same] = res[same] * (1 - a) + prev_out[same].astype(np.float32) * a
                    res = res.astype(np.uint8)
                return res
        return process_frame(fr, m, self.s.mode)

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
