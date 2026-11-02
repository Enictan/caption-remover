"""
Runs inside the Voice engine's own Python environment (not the main app's).

    python voice_worker.py serve --port 17600 --data-dir DIR --parent PID
        Starts the Voicebox server (its source folder is the working directory) and
        shuts it down if Caption Remover quits or crashes.

    python voice_worker.py transcribe AUDIO.wav --model turbo [--language en]
        Speech to text WITH timestamps (Voicebox's own /transcribe returns text only).
        Progress lines go to stderr; the final JSON {"segments": [...]} goes to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

WHISPER = {
    "fast": "openai/whisper-small",
    "turbo": "openai/whisper-large-v3-turbo",
}


def _watch_parent(pid: int):
    while True:
        time.sleep(2)
        try:
            os.kill(pid, 0)
        except OSError:
            os._exit(0)


def serve(a):
    sys.path.insert(0, os.getcwd())          # Voicebox source folder
    if a.parent:
        threading.Thread(target=_watch_parent, args=(a.parent,), daemon=True).start()
    import uvicorn
    from backend import config, database
    if a.data_dir:
        config.set_data_dir(a.data_dir)
    database.init_db()
    from backend.app import app
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def transcribe(a):
    import torch
    from transformers import pipeline

    if torch.cuda.is_available():
        device, dtype = "cuda:0", torch.float16
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device, dtype = "mps", torch.float16
    else:
        device, dtype = "cpu", torch.float32
    import soundfile as sf
    audio, sr = sf.read(a.audio, dtype="float32")   # 16 kHz mono WAV made by the app
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    repo = WHISPER.get(a.model, a.model)
    say(f"PROGRESS 0.05 Loading speech model ({repo.split('/')[-1]}) on {device}")

    def run(dev, dt):
        asr = pipeline("automatic-speech-recognition", model=repo, torch_dtype=dt, device=dev)
        say("PROGRESS 0.25 Listening")
        gk = {"task": "transcribe"}
        if a.language:
            gk["language"] = a.language
        return asr({"raw": audio, "sampling_rate": sr}, chunk_length_s=30, batch_size=4,
                   return_timestamps=True, generate_kwargs=gk)

    try:
        out = run(device, dtype)
    except Exception as e:  # MPS half precision can fail on some ops: retry the safe way
        if device == "cpu":
            raise
        say(f"PROGRESS 0.2 Retrying on CPU ({e.__class__.__name__})")
        out = run("cpu", torch.float32)

    segs = []
    for c in out.get("chunks") or []:
        s, e = (c.get("timestamp") or (None, None))
        text = (c.get("text") or "").strip()
        if not text:
            continue
        s = float(s) if s is not None else (segs[-1]["end"] if segs else 0.0)
        e = float(e) if e is not None else s + max(1.0, len(text) / 15)
        segs.append({"start": round(s, 3), "end": round(max(e, s + 0.2), 3), "text": text})
    if not segs and (out.get("text") or "").strip():
        segs = [{"start": 0.0, "end": float(a.duration or 0) or 1.0, "text": out["text"].strip()}]
    say("PROGRESS 1.0 Done")
    print(json.dumps({"segments": segs, "model": repo, "device": device}))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--data-dir")
    s.add_argument("--parent", type=int)
    t = sub.add_parser("transcribe")
    t.add_argument("audio")
    t.add_argument("--model", default="turbo")
    t.add_argument("--language")
    t.add_argument("--duration", type=float)
    a = p.parse_args()
    serve(a) if a.cmd == "serve" else transcribe(a)


if __name__ == "__main__":
    main()
