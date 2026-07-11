#!/usr/bin/env python3
"""
captions.py

Generates burned-in captions for a specific clip time range from the word-level
transcript produced by transcribe.py, then burns them onto the video with ffmpeg.

Two styles:
  - "animated" (default): the CapCut/Opus-style word-pop -- a few words sit on
    screen at a time, and whichever word is being spoken right now pops in a
    highlight color and scales up briefly. This is the eye-catchy one.
  - "simple": one static line per chunk, no per-word animation -- lighter
    weight, useful if you want something calmer.

No external calls -- pure local ffmpeg/libass.

Usage:
    python captions.py clip.mp4 transcript.json --clip-start 12.4 --clip-end 73.1 -o final.mp4
    python captions.py clip.mp4 transcript.json --clip-start 12.4 --clip-end 73.1 -o final.mp4 --style simple
"""

import argparse
import json
import re
import shutil
import subprocess


def _format_ass_time(seconds: float) -> str:
    """ASS timestamps are H:MM:SS.CC"""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def collect_words_in_range(segments, clip_start, clip_end):
    """Flattens word-level timestamps from segments, keeping only words inside
    [clip_start, clip_end], shifted to be relative to clip_start."""
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            if w["end"] <= clip_start or w["start"] >= clip_end:
                continue
            words.append({
                "start": max(0.0, w["start"] - clip_start),
                "end": max(0.0, w["end"] - clip_start),
                "word": w["word"],
            })
    return words


def chunk_words(words, words_per_chunk=5, max_gap=1.2):
    """Groups consecutive words into short caption lines (the classic 'few words
    at a time, centered' caption style). Keeps the per-word list too, since the
    animated style needs individual word timings within each chunk.

    Also starts a fresh chunk whenever there's a silence longer than `max_gap`
    seconds between words -- otherwise one caption line straddles the pause,
    mixing the end of one sentence with the start of the next and sitting
    frozen on screen through the silence."""
    chunks = []
    group = []

    def flush():
        if group:
            chunks.append({
                "start": group[0]["start"],
                "end": group[-1]["end"],
                "text": " ".join(w["word"] for w in group),
                "words": list(group),
            })
            group.clear()

    for w in words:
        if not w["word"]:
            continue
        if group and (len(group) >= words_per_chunk
                      or w["start"] - group[-1]["end"] > max_gap):
            flush()
        group.append(w)
    flush()
    return chunks


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,72,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


HIGHLIGHT_COLOR = "&H00FFFF&"  # ASS is BGR -- this is yellow (B=00,G=FF,R=FF)


def _sanitize(text: str) -> str:
    """Braces start ASS override blocks -- a transcript word containing one
    would corrupt the rendering of every caption after it."""
    return text.replace("{", "(").replace("}", ")")


def build_ass(chunks) -> str:
    """Static style: one plain line per chunk, no per-word animation."""
    lines = [ASS_HEADER]
    for c in chunks:
        start = _format_ass_time(c["start"])
        end = _format_ass_time(c["end"])
        text = _sanitize(c["text"].upper().replace("\n", " "))
        lines.append(f"Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}")
    return "\n".join(lines)


def build_ass_animated(chunks) -> str:
    """Word-pop style: the whole chunk stays on screen, but whichever word is
    being spoken right now is highlighted in color and pops up in scale, then
    settles back down -- one Dialogue event per word, each showing the full
    chunk text with that word's run recolored/animated."""
    lines = [ASS_HEADER]
    for c in chunks:
        words = c["words"]
        for i, w in enumerate(words):
            start = w["start"]
            # extend to the next word's start so there's no gap/flicker
            end = words[i + 1]["start"] if i + 1 < len(words) else w["end"]
            if end <= start:
                end = start + 0.05

            parts = []
            for j, ow in enumerate(words):
                token = _sanitize(ow["word"].upper())
                if j == i:
                    parts.append(
                        "{\\c" + HIGHLIGHT_COLOR + "}"
	   	        + token + "{\\r}"
                    )
                else:
                    parts.append(token)
            text = " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},"
                f"Caption,,0,0,0,,{text}"
            )
    return "\n".join(lines)


_BURN_FFMPEG = "unresolved"


def _burn_ffmpeg():
    """Returns the first ffmpeg binary whose build has the libass 'ass'
    filter, or None. Homebrew's current ffmpeg bottle dropped libass, but
    the keg-only ffmpeg@7 bottle still has it, so the common keg paths are
    checked after PATH."""
    global _BURN_FFMPEG
    if _BURN_FFMPEG == "unresolved":
        _BURN_FFMPEG = None
        candidates = [
            "ffmpeg",
            "/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg",   # Apple Silicon brew keg
            "/usr/local/opt/ffmpeg@7/bin/ffmpeg",      # Intel mac brew keg
        ]
        for candidate in candidates:
            try:
                out = subprocess.run(
                    [candidate, "-hide_banner", "-filters"],
                    capture_output=True, text=True, timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if re.search(r"\s(ass)\s+V->V", out.stdout):
                _BURN_FFMPEG = candidate
                break
    return _BURN_FFMPEG


def burn_captions(video_path: str, ass_path: str, output_path: str, ffmpeg_bin: str = "ffmpeg"):
    """Burns the .ass subtitle file into the video using ffmpeg/libass."""
    escaped = ass_path.replace("\\", "\\\\").replace(":", "\\:")
    cmd = [
        ffmpeg_bin, "-y", "-i", video_path,
        "-vf", f"ass={escaped}",
        "-c:v", "libx264", "-c:a", "copy",
        "-loglevel", "error",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def add_captions(video_path, transcript_segments, clip_start, clip_end, output_path,
                  words_per_chunk=5, style="animated"):
    """High-level entry point used by make_shorts.py."""
    words = collect_words_in_range(transcript_segments, clip_start, clip_end)
    chunks = chunk_words(words, words_per_chunk)
    ass_content = build_ass_animated(chunks) if style == "animated" else build_ass(chunks)

    ass_path = output_path.rsplit(".", 1)[0] + ".ass"
    with open(ass_path, "w") as f:
        f.write(ass_content)

    ffmpeg_bin = _burn_ffmpeg()
    if ffmpeg_bin is None:
        # A short without burned captions beats no short at all. Keep the
        # .ass sidecar so the captions aren't lost, and say how to get them
        # burned in.
        print("    (no ffmpeg build with libass found -- saving the short "
              "WITHOUT burned captions; subtitles kept next to it as .ass.\n"
              "     On macOS: brew install ffmpeg@7)")
        shutil.copyfile(video_path, output_path)
        return len(chunks)
    burn_captions(video_path, ass_path, output_path, ffmpeg_bin)
    return len(chunks)


def main():
    parser = argparse.ArgumentParser(description="Burn word-synced captions into a clip.")
    parser.add_argument("video_path")
    parser.add_argument("transcript_json", help="Full transcript.json from transcribe.py")
    parser.add_argument("--clip-start", type=float, required=True)
    parser.add_argument("--clip-end", type=float, required=True)
    parser.add_argument("--words-per-chunk", type=int, default=5)
    parser.add_argument("--style", choices=["animated", "simple"], default="animated")
    parser.add_argument("-o", "--output", default="captioned.mp4")
    args = parser.parse_args()

    with open(args.transcript_json) as f:
        segments = json.load(f)

    n = add_captions(args.video_path, segments, args.clip_start, args.clip_end,
                      args.output, args.words_per_chunk, args.style)
    print(f"Wrote {args.output} ({n} caption lines)")


if __name__ == "__main__":
    main()
