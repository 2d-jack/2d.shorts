#!/usr/bin/env python3
"""
transcribe.py

GPU-accelerated transcription with word-level timestamps, using faster-whisper.
Outputs a JSON list of segments compatible with highlight_selector.py and captions.py:

[
  {
    "start": 0.0, "end": 4.2, "text": "...",
    "words": [{"start": 0.0, "end": 0.3, "word": "So"}, ...]
  },
  ...
]

On your server (GPU):
    python transcribe.py audio.wav --model large-v3 --device cuda --compute-type float16 -o transcript.json

No API keys, no external calls -- this runs entirely on local hardware.
"""

import argparse
import json
import os

from faster_whisper import WhisperModel


def transcribe(audio_path, model_size="large-v3", device="cuda", compute_type="float16"):
    # faster-whisper defaults to 4 CPU threads, which leaves a modern CPU
    # ~90% idle during the slowest step of the whole pipeline. Use every
    # core when transcribing on CPU (harmless on GPU: the value is ignored).
    cpu_threads = os.cpu_count() or 4
    model = WhisperModel(model_size, device=device, compute_type=compute_type,
                         cpu_threads=cpu_threads)
    segments_gen, info = model.transcribe(audio_path, word_timestamps=True)

    segments = []
    for seg in segments_gen:
        words = [
            {"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()}
            for w in (seg.words or [])
        ]
        segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
            "words": words,
        })
    return segments


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio with word-level timestamps using faster-whisper."
    )
    parser.add_argument("audio_path")
    parser.add_argument("--model", default="large-v3",
                         help="Whisper model size: tiny/base/small/medium/large-v3 (default: large-v3)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"], help="default: cuda")
    parser.add_argument("--compute-type", default="float16",
                         help="float16 on GPU, int8 on CPU (default: float16)")
    parser.add_argument("-o", "--output", default="transcript.json")
    args = parser.parse_args()

    segments = transcribe(args.audio_path, args.model, args.device, args.compute_type)

    with open(args.output, "w") as f:
        json.dump(segments, f, indent=2)

    print(f"Wrote {len(segments)} segments to {args.output}")


if __name__ == "__main__":
    main()
