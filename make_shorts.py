#!/usr/bin/env python3
"""
make_shorts.py

Give it a YouTube URL (or a local video file), get back ranked, captioned,
vertical short clips -- the same job OpusClip/Wayin do, running entirely on
your own GPU. No API keys, no per-clip cost.

Pipeline:
    1. Download (yt-dlp)            -- skipped if you pass a local file
    2. Transcribe (faster-whisper)  -- GPU, word-level timestamps
    3. Pick highlights (local LLM)  -- via Ollama, see highlight_selector.py
    4. Reframe to 9:16              -- per-scene crop-or-blur (hybrid), or a
                                        single whole-clip mode -- see smart_crop.py
    5. Burn in captions             -- ffmpeg/libass
    6. Render final clips

One-time setup on your server:
    pip install yt-dlp faster-whisper opencv-python-headless
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull gemma4:12b
    # optional, for better reframing inside smart_crop mode:
    git clone https://github.com/KazKozDev/auto-vertical-reframe.git
    cd auto-vertical-reframe && pip install -e .
    # optional, for more accurate scene-cut detection in hybrid mode:
    pip install scenedetect --break-system-packages

Usage:
    python make_shorts.py "https://youtube.com/watch?v=..."
        (no --num-clips or -m given -> prompts you interactively for a clip count)
    python make_shorts.py "https://youtube.com/watch?v=..." --num-clips 3
    python make_shorts.py "https://youtube.com/watch?v=..." -m "the part where they argue about the budget"
        (finds that one specific moment instead of ranking the best N clips)
    python make_shorts.py /path/to/local_video.mp4 --num-clips 3

    # Iterate on captions/cropping without re-running whisper each time:
    python make_shorts.py video.mp4 --transcript-json transcript.json
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

from highlight_selector import (select_highlights, select_moment,
                                is_thinking_model, unload_model, stop_ollama,
                                DEFAULT_TIMEOUT as LLM_DEFAULT_TIMEOUT)
from smart_crop import reframe
from captions import add_captions
from paths import load_config


def resolve_whisper_engine(requested: str) -> str:
    """The transcription engine ladder, decided per machine:
      1. cuda   -- NVIDIA GPU with working CUDA -> faster-whisper on GPU
      2. vulkan -- any other GPU (AMD/Intel/Apple) -> whisper.cpp's Vulkan/
                   Metal backend, binary auto-downloaded from our own release
      3. cpu    -- no usable GPU -> faster-whisper on every CPU core
    The vulkan rung is optimistic: if it fails at runtime (odd driver, out of
    VRAM), run_transcription logs why and falls through to cpu -- a bad GPU
    never kills a job."""
    if requested != "auto":
        return requested
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    try:
        import whispercpp
        if whispercpp.usable():
            return "vulkan"
    except Exception:
        pass
    return "cpu"


def resolve_whisper_settings(device: str, compute_type: str, model: str):
    """Turns "auto" into real values based on the hardware this machine has:
    an NVIDIA GPU gets cuda/float16/large-v3, everything else (Macs, GPU-less
    boxes) gets cpu/int8/small so transcription stays usable without CUDA."""
    if device == "auto":
        has_cuda = False
        try:
            import ctranslate2
            has_cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            pass
        device = "cuda" if has_cuda else "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    if not model:
        model = "large-v3" if device == "cuda" else "small"
    return device, compute_type, model


def is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def ytdlp_argv():
    """yt-dlp is normally pip-installed into the venv this script runs in,
    and that venv's Scripts/bin dir isn't on PATH when the launcher or the
    web UI invokes this interpreter directly (nothing "activates" the venv).
    Running the module through the current interpreter sidesteps PATH
    entirely; a yt-dlp binary on PATH is the fallback for people who
    installed it globally. Returns None if neither exists."""
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return None


def download_video(url: str, workdir: str, cookies_file: str = None) -> str:
    out_path = os.path.join(workdir, "source.mp4")
    cmd = ytdlp_argv() + [
        "--js-runtimes", "node",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", out_path,
    ]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    cmd.append(url)
    subprocess.run(cmd, check=True)
    # yt-dlp exits 0 on channel/playlist pages that yield zero videos; catch
    # that here so the user sees the real problem instead of ffmpeg failing
    # on a missing file several steps later.
    if not os.path.exists(out_path):
        sys.exit(f"Download finished but no video file was produced.\n"
                 f"The URL probably doesn't point at a single video -- check it "
                 f"for typos or stray characters: {url}")
    return out_path


def extract_audio(video_path: str, workdir: str) -> str:
    audio_path = os.path.join(workdir, "audio.wav")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-loglevel", "error",
        audio_path,
    ]
    subprocess.run(cmd, check=True)
    return audio_path


def run_transcription(audio_path: str, model: str, device: str, compute_type: str,
                      workdir: str, engine: str = "cpu"):
    segments = None
    if engine == "vulkan":
        try:
            import whispercpp
            segments = whispercpp.transcribe(audio_path, model_size=model)
        except Exception as e:
            print(f"  GPU (Vulkan) transcription didn't work on this machine: {e}")
            print("  Falling back to CPU transcription -- the job continues.")
    if segments is None:
        from transcribe import transcribe  # imported lazily so --transcript-json users
                                            # don't need faster-whisper installed at all
        segments = transcribe(audio_path, model_size=model, device=device,
                              compute_type=compute_type)
    transcript_path = os.path.join(workdir, "transcript.json")
    with open(transcript_path, "w") as f:
        json.dump(segments, f, indent=2)
    return segments


def probe_duration(video_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "csv=p=0", video_path]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def extract_subclip(source_path: str, start: float, end: float, out_path: str):
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-ss", str(start), "-to", str(end),
        "-c:v", "libx264", "-c:a", "aac",
        "-loglevel", "error",
        out_path,
    ]
    subprocess.run(cmd, check=True)


def check_dependency(name: str, hint: str):
    if shutil.which(name) is None:
        sys.exit(f"Missing dependency: '{name}' not found on PATH.\n  {hint}")


# ---------------------------------------------------------------------------
# Stored-job management (--list-jobs / --clear-job / --clear-jobs)
#
# "Jobs" here means the per-run folders under <data dir>/jobs -- the same
# ones the web UI creates and lists. CLI runs that used a custom
# --output-dir live wherever you pointed them and aren't tracked here.
# ---------------------------------------------------------------------------

def jobs_dir() -> str:
    from paths import data_dir
    return os.path.join(data_dir(), "jobs")


def iter_jobs():
    jd = jobs_dir()
    if not os.path.isdir(jd):
        return
    for job_id in sorted(os.listdir(jd)):
        path = os.path.join(jd, job_id)
        if os.path.isdir(path):
            yield job_id, path


def job_stats(path: str):
    n_clips, size = 0, 0
    for _root, _dirs, files in os.walk(path):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(_root, f))
            except OSError:
                pass
            if re.match(r"short_\d+\.mp4$", f):
                n_clips += 1
    return n_clips, size


def list_jobs_cmd():
    rows = [(job_id, *job_stats(path)) for job_id, path in iter_jobs()]
    if not rows:
        print(f"No stored jobs in {jobs_dir()}")
        return
    print(f"Stored jobs in {jobs_dir()}:\n")
    for job_id, n_clips, size in rows:
        print(f"  {job_id}   {n_clips} clip(s)   {size / 1048576:.1f} MB")
    print("\nDelete one with:  shorts --clear-job <JOB_ID>")
    print("Delete all with:  shorts --clear-jobs")


def clear_jobs_cmd(job_ids, clear_all: bool):
    targets = [job_id for job_id, _ in iter_jobs()] if clear_all else (job_ids or [])
    if not targets:
        print("No stored jobs to clear." if clear_all
              else "No job id given (see: shorts --list-jobs)")
        return
    for job_id in targets:
        path = os.path.join(jobs_dir(), job_id)
        # basename() blocks path tricks like --clear-job ..\..\something
        if os.path.basename(job_id) != job_id or not os.path.isdir(path):
            print(f"  {job_id}: no such job (see: shorts --list-jobs)")
            continue
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            print(f"  {job_id}: some files are still in use (is the job still "
                  f"running?) -- not fully deleted")
        else:
            print(f"  {job_id}: deleted")


def prompt_for_clip_count(default=3) -> int:
    while True:
        try:
            raw = input("How many clips do you want? ").strip()
        except EOFError:
            print(f"\nNo input available (non-interactive run) -- defaulting to {default}. "
                  f"Pass --num-clips to set this explicitly next time.")
            return default
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        print("Please enter a positive whole number.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter,
                                      add_help=False)
    parser.add_argument("--help", action="help", help="show this help message and exit")
    parser.add_argument("input", nargs="?", default=None,
                         help="YouTube URL or path to a local video file")
    parser.add_argument("--output-dir", default="./shorts_output")
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument("-m", "--moment", default=None,
                                  help="Find one specific moment instead of ranking the best clips. "
                                       "Describe what you're looking for, e.g. "
                                       "-m \"the part where they argue about the budget\". "
                                       "Produces exactly one clip. Can't be combined with --num-clips.")
    selection_group.add_argument("--num-clips", type=int, default=None,
                                  help="How many clips to produce. If omitted (and -m/--moment isn't "
                                       "used either), you'll be prompted for this interactively.")
    parser.add_argument("--clip-duration", type=int, default=60,
                         help="Target clip length in seconds (default: 60)")
    parser.add_argument("--words-per-caption", type=int, default=5)

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("-v", "--vertical", action="store_true",
                             help="Force TikTok-style vertical crop for the whole clip "
                                  "(same as --reframe-mode smart_crop).")
    mode_group.add_argument("-h", "--horizontal-blur", action="store_true",
                             help="Force full-frame with blurred top/bottom padding for the "
                                  "whole clip, no cropping (same as --reframe-mode blur_letterbox).")
    parser.add_argument("--reframe-mode", choices=["hybrid", "smart_crop", "blur_letterbox"],
                         default="hybrid",
                         help="Used only if neither -v nor -h is given. hybrid: per-scene "
                              "crop-or-blur decision, fixes cropping into B-roll/crowds (default). "
                              "smart_crop: one crop treatment for the whole clip. "
                              "blur_letterbox: keep the full frame, blur-fill top/bottom for the whole clip.")
    parser.add_argument("--caption-style", choices=["animated", "simple"], default="animated",
                         help="animated: word-by-word pop highlight (default). simple: static line per chunk.")
    parser.add_argument("--whisper-model", default=None,
                         help="Whisper model size. Default: large-v3 on an NVIDIA GPU, "
                              "small on CPU/Apple Silicon.")
    parser.add_argument("--whisper-device", default="auto", choices=["auto", "cuda", "cpu"],
                         help="Default: auto -- cuda if an NVIDIA GPU is available, else cpu.")
    parser.add_argument("--whisper-engine", default="auto",
                         choices=["auto", "cuda", "vulkan", "cpu"],
                         help="Transcription engine. Default: auto -- NVIDIA GPUs use "
                              "faster-whisper on CUDA, other GPUs (AMD/Intel) use "
                              "whisper.cpp's Vulkan backend, no GPU means CPU. "
                              "vulkan falls back to cpu automatically if it fails.")
    parser.add_argument("--whisper-compute-type", default="auto",
                         help="Default: auto -- float16 on cuda, int8 on cpu.")
    parser.add_argument("--llm-model", default=None,
                         help="Ollama model tag. Default: the model chosen at install time "
                              "(config.json), or gemma4:12b.")
    parser.add_argument("--llm-base-url", default=None,
                         help="Ollama's native API base. Default: config.json's llm_base_url, "
                              "or http://localhost:11434.")
    parser.add_argument("--llm-timeout", type=int, default=None,
                         help="Seconds to wait for the highlight-picking LLM before giving "
                              "up. Default: config.json's llm_timeout, or "
                              f"{LLM_DEFAULT_TIMEOUT}. Raise this on slow machines.")
    parser.add_argument("--stop-ollama", action="store_true",
                         help="Fully stop the Ollama background process when the job "
                              "finishes (default: only unload the model from memory, "
                              "which frees the RAM/VRAM but keeps Ollama ready for the "
                              "next job). Can also be set via config.json's "
                              "stop_ollama_after_job: true.")
    parser.add_argument("--think", action="store_true",
                         help="Let the highlight-ranking model think before answering "
                              "(slower, off by default; only matters for thinking-capable "
                              "models like Qwen3.5)")
    parser.add_argument("--transcript-json", default=None,
                         help="Skip download+transcription and reuse an existing transcript "
                              "(still needs the source video for cropping clips out of)")
    parser.add_argument("--keep-temp", action="store_true",
                         help="Don't delete the working directory when done")
    parser.add_argument("--cookies", default=None,
                         help="Path to a cookies.txt file for YouTube authentication")

    jobs_group = parser.add_argument_group(
        "stored-job management",
        "Every run started from the web UI is stored as a job under the app's "
        "data folder. These commands list/delete them and exit -- no video is "
        "processed.")
    jobs_group.add_argument("--list-jobs", action="store_true",
                             help="List stored jobs (id, clip count, size) and exit")
    jobs_group.add_argument("--clear-job", metavar="JOB_ID", action="append",
                             help="Delete one stored job and its clips. Repeatable. "
                                  "Get ids from --list-jobs or the web UI job list.")
    jobs_group.add_argument("--clear-jobs", action="store_true",
                             help="Delete ALL stored jobs and their clips")
    args = parser.parse_args()

    if args.list_jobs or args.clear_job or args.clear_jobs:
        if args.list_jobs:
            list_jobs_cmd()
        if args.clear_job or args.clear_jobs:
            clear_jobs_cmd(args.clear_job, clear_all=args.clear_jobs)
        return

    if not args.input:
        parser.error("input is required (a YouTube URL or a local video file)")

    # Shell tab-completion escapes ? and = in pasted URLs, and inside quotes
    # those backslashes reach us literally (watch\?v\=...). Backslash is never
    # a valid character in a video URL, so strip them instead of letting
    # yt-dlp chase a mangled address through its generic extractor.
    if is_url(args.input):
        args.input = args.input.replace("\\", "")

    if args.vertical:
        args.reframe_mode = "smart_crop"
    elif args.horizontal_blur:
        args.reframe_mode = "blur_letterbox"
    # else: leave args.reframe_mode as whatever --reframe-mode said (default "hybrid")

    # Fill LLM defaults from the per-device config the installer wrote
    cfg = load_config()
    args.llm_model = args.llm_model or cfg.get("llm_model") or "gemma4:12b"
    args.llm_base_url = args.llm_base_url or cfg.get("llm_base_url") or "http://localhost:11434"
    args.llm_timeout = args.llm_timeout or cfg.get("llm_timeout") or LLM_DEFAULT_TIMEOUT
    args.stop_ollama = args.stop_ollama or bool(cfg.get("stop_ollama_after_job"))

    if is_thinking_model(args.llm_model) and not args.think:
        print(f"Note: {args.llm_model} needs thinking mode to answer -- enabling it automatically.")
        args.think = True

    # Resolve the engine ladder, then the "auto" whisper settings.
    # vulkan pins the fallback device to cpu so a mid-job engine failure
    # lands somewhere that always works; model defaults follow the device
    # (small on cpu/vulkan -- right-sized for low-end GPUs' VRAM too).
    engine = resolve_whisper_engine(args.whisper_engine)
    if engine == "cuda":
        args.whisper_device = "cuda"
    elif engine in ("vulkan", "cpu"):
        args.whisper_device = "cpu"
    device, compute, wmodel = resolve_whisper_settings(
        args.whisper_device, args.whisper_compute_type, args.whisper_model)
    args.whisper_device, args.whisper_compute_type, args.whisper_model = device, compute, wmodel

    if args.moment:
        num_clips = 1
    elif args.num_clips is not None:
        num_clips = args.num_clips
    else:
        num_clips = prompt_for_clip_count()

    check_dependency("ffmpeg", "Install with: sudo apt install ffmpeg")
    if is_url(args.input) and ytdlp_argv() is None:
        sys.exit("Missing dependency: yt-dlp is neither installed in this Python "
                 "environment nor on PATH.\n  Install with: pip install yt-dlp")

    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.output_dir, run_id)
    workdir = os.path.join(run_dir, "_work")
    os.makedirs(workdir, exist_ok=True)

    # 1. Get the source video
    if is_url(args.input):
        print(f"Downloading: {args.input}")
        source_path = download_video(args.input, workdir, cookies_file=args.cookies)
    else:
        source_path = args.input
        if not os.path.exists(source_path):
            sys.exit(f"Local file not found: {source_path}")

    # 2. Transcript (or reuse one)
    if args.transcript_json:
        print(f"Reusing transcript: {args.transcript_json}")
        with open(args.transcript_json) as f:
            segments = json.load(f)
    else:
        engine_desc = {"cuda": "faster-whisper on NVIDIA GPU",
                       "vulkan": "whisper.cpp on GPU via Vulkan",
                       "cpu": "faster-whisper on CPU"}[engine]
        print(f"Extracting audio + transcribing locally "
              f"(whisper {args.whisper_model}, {engine_desc})...")
        audio_path = extract_audio(source_path, workdir)
        segments = run_transcription(
            audio_path, args.whisper_model, args.whisper_device,
            args.whisper_compute_type, workdir, engine=engine,
        )
        print(f"  -> {len(segments)} segments")

    # 3. Pick highlights with the local LLM. Whatever happens, free the
    # model's RAM/VRAM afterwards -- Ollama otherwise keeps it loaded for
    # minutes, which pins a low-end machine's memory even after a crash.
    try:
        if args.moment:
            print(f"Asking local LLM ({args.llm_model}) to find: \"{args.moment}\"...")
            highlights = select_moment(
                segments, args.moment, model=args.llm_model, target_duration=args.clip_duration,
                base_url=args.llm_base_url, think=args.think, timeout=args.llm_timeout,
            )
        else:
            print(f"Asking local LLM ({args.llm_model}) for the best {num_clips} clips...")
            highlights = select_highlights(
                segments, model=args.llm_model, target_duration=args.clip_duration,
                num_clips=num_clips, base_url=args.llm_base_url, think=args.think,
                timeout=args.llm_timeout,
            )
    finally:
        unload_model(args.llm_model, args.llm_base_url)
        if args.stop_ollama:
            stop_ollama()

    # Clamp highlight timestamps to the source video's real duration -- the
    # LLM occasionally hallucinates a range past the end of the video, which
    # would otherwise produce an empty/corrupt clip and crash deep inside
    # the reframing step with a confusing error.
    source_duration = probe_duration(source_path)
    valid_highlights = []
    for clip in highlights:
        # The LLM sometimes returns timestamps as strings ("12.4") or leaves
        # a field out entirely -- coerce before comparing, don't crash.
        try:
            start, end = float(clip["start"]), float(clip["end"])
        except (KeyError, TypeError, ValueError):
            print(f"  Skipping a highlight with malformed timestamps: {clip!r}")
            continue
        end = min(end, source_duration)
        if start >= end or start >= source_duration:
            print(f"  Skipping a highlight ({start:.1f}s-{end:.1f}s): "
                  f"outside the source video's actual duration ({source_duration:.1f}s)")
            continue
        valid_highlights.append({**clip, "start": start, "end": end})

    # 4-6. Extract, reframe, caption each highlight
    results = []
    for i, clip in enumerate(valid_highlights, start=1):
        start, end = clip["start"], clip["end"]
        print(f"\nClip {i}/{len(valid_highlights)}: {start:.1f}s - {end:.1f}s -- {clip.get('reason', '')}")

        raw_path = os.path.join(workdir, f"clip_{i}_raw.mp4")
        vertical_path = os.path.join(workdir, f"clip_{i}_vertical.mp4")
        final_path = os.path.join(run_dir, f"short_{i}.mp4")

        try:
            print("  extracting subclip...")
            extract_subclip(source_path, start, end, raw_path)

            print("  reframing to 9:16...")
            method = reframe(raw_path, vertical_path, mode=args.reframe_mode)
            print(f"    ({method})")

            print("  burning captions...")
            n_lines = add_captions(vertical_path, segments, start, end, final_path,
                                    words_per_chunk=args.words_per_caption, style=args.caption_style)
            print(f"    ({n_lines} caption lines)")
        except Exception as e:
            print(f"  FAILED on this clip, skipping it and continuing: {e}")
            continue

        results.append(final_path)

    if not args.keep_temp:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"\nDone. {len(results)} shorts written to {run_dir}/:")
    for path in results:
        print(f"  {path}")


if __name__ == "__main__":
    main()
