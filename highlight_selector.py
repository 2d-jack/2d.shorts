#!/usr/bin/env python3
"""
highlight_selector.py

Fully local replacement for the OpenAI-based highlight-selection step used by
tools like AI-Youtube-Shorts-Generator. Takes a Whisper/WhisperX-style transcript
(a list of {start, end, text} segments) and asks a local LLM served by Ollama
to pick the best clip-worthy windows.

No API keys, no external calls, no cost beyond your own electricity.

Setup on your server:
    1. Install Ollama: curl -fsSL https://ollama.com/install.sh | sh
    2. ollama pull gemma4:12b       # ~8GB, comfortable on a single A100
    3. ollama serve   (usually runs automatically as a systemd service after install)

Usage:
    python highlight_selector.py transcript.json --model gemma4:12b

Where transcript.json looks like:
    [
      {"start": 0.0, "end": 4.2, "text": "So the first thing nobody tells you..."},
      {"start": 4.2, "end": 9.8, "text": "...is that it actually breaks in production."},
      ...
    ]

This is exactly the segment format faster-whisper / WhisperX produce, so you can
pipe their output straight into this with minimal glue code.

Note on thinking models: Qwen3.5 (and Qwen3, DeepSeek-R1, etc.) default to
"thinking mode" -- a long <think>...</think> reasoning block before the real
answer. This module talks to Ollama's *native* /api/chat endpoint (not the
OpenAI-compatible /v1 one) and sends think=False, because Ollama only honors
that setting on the native endpoint. There's also a defensive regex strip of
any <think> block that slips through anyway, just in case.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 600  # seconds; low-end machines legitimately need minutes


def is_thinking_model(model: str) -> bool:
    """DeepSeek-R1 variants produce no usable answer unless thinking is
    enabled, so callers force think=True for them regardless of flags."""
    return model.lower().startswith("deepseek-r1")


def unload_model(model: str, base_url: str = "http://localhost:11434"):
    """Asks Ollama to evict the model from RAM/VRAM right now (it otherwise
    keeps it loaded for several minutes after the last request). Best-effort:
    a dead/unreachable Ollama is not an error worth failing a finished job over."""
    payload = {"model": model, "keep_alive": 0}
    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        print(f"  (unloaded {model} from Ollama's memory)")
    except Exception:
        pass


def stop_ollama():
    """Fully stops the Ollama background process (opt-in via --stop-ollama /
    config). Best-effort and platform-specific; on Linux systemd installs the
    service may auto-restart, which is why unload_model is the default and
    this is optional."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", "ollama app.exe", "/F"], capture_output=True)
            subprocess.run(["taskkill", "/IM", "ollama.exe", "/F"], capture_output=True)
        else:
            subprocess.run(["pkill", "-x", "ollama"], capture_output=True)
        print("  (stopped the Ollama background process)")
    except Exception:
        pass


def call_local_llm(prompt: str, model: str, base_url: str = "http://localhost:11434",
                    timeout: int = DEFAULT_TIMEOUT, think: bool = False) -> str:
    """Calls Ollama's native /api/chat endpoint (not the OpenAI-compat /v1 one).

    Why native and not /v1: thinking-capable models (Qwen3.5, Qwen3, DeepSeek-R1,
    etc.) default to emitting a long <think>...</think> block before the real
    answer. The only reliable way to turn that off is the `think` field, and
    Ollama's docs note the OpenAI-compatible /v1 endpoint drops that option --
    only the native endpoint honors it. Without this, a thinking model will
    either blow your latency budget or return content that isn't valid JSON.
    """
    # Ollama's default context window (~4k tokens) silently truncates long
    # transcripts AND cuts generation off mid-answer once the window fills,
    # which surfaces as "model didn't return valid JSON". Size the window to
    # the prompt (~3 chars/token is a safe overestimate) plus generous room
    # for the answer; Ollama caps it to the model's real maximum itself.
    num_ctx = min(len(prompt) // 3 + 2048, 32768)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0.4, "num_ctx": num_ctx},
        "stream": False,
        "think": think,
    }
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["message"]["content"]
    except (TimeoutError, socket.timeout):
        raise RuntimeError(
            f"The local LLM ({model}) didn't answer within {timeout} seconds.\n"
            f"  This usually means the model is too big for this machine's "
            f"hardware, so it's generating extremely slowly.\n"
            f"  Fixes, in order of effectiveness:\n"
            f"    1. Use a smaller model: ollama pull qwen3.5:9b   (or "
            f"deepseek-r1:1.5b for very weak machines), then pick it in the "
            f"web UI's Advanced box or pass --llm-model\n"
            f"    2. Give it more time: pass --llm-timeout 1200 (seconds)\n"
            f"    3. Close other RAM/GPU-hungry programs and retry"
        )
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            raise RuntimeError(
                f"The local LLM ({model}) didn't answer within {timeout} seconds "
                f"-- likely too big for this machine. Try a smaller model "
                f"(qwen3.5:9b or deepseek-r1:1.5b) or raise --llm-timeout."
            )
        raise RuntimeError(
            f"Couldn't reach local LLM at {base_url}. "
            f"Is Ollama running? Try `ollama serve` or `systemctl status ollama`. "
            f"Original error: {e}"
        )
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Unexpected response shape from {base_url}: {e}")


def build_prompt(transcript_segments, target_duration=60, num_clips=3) -> str:
    """Builds the highlight-selection prompt from a Whisper-style transcript."""
    lines = [
        f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text'].strip()}"
        for seg in transcript_segments
    ]
    transcript_text = "\n".join(lines)

    return f"""You are selecting the best short-form clip segments from a video transcript for vertical social media (TikTok/Reels/Shorts).

Transcript (timestamps in seconds):
{transcript_text}

Pick the {num_clips} most engaging, self-contained segments. Each should:
- Run roughly {target_duration} seconds (a complete thought: a hook, a story, a punchline, a strong claim)
- Make sense without the rest of the video
- Be ranked by how likely it is to grab attention in the first 3 seconds

Respond with ONLY a JSON array, no other text, no markdown fences, in this exact format:
[
  {{"start": 12.4, "end": 73.1, "reason": "short reason this works as a standalone clip"}}
]"""


def _strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return cleaned.strip()


def _strip_thinking_block(text: str) -> str:
    """Defense in depth: even with think=False, strip a <think>...</think>
    block that slips through (Qwen-style models)."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json_array(text: str) -> str:
    """Finds the first top-level JSON array in the text and returns just
    that substring, ignoring anything before or after it.

    Different reasoning-capable models wrap their answer in different
    thinking-trace formats -- Qwen uses <think>...</think>, Gemma 4 wraps it
    in a channel marker that's still emitted (empty) even with thinking
    disabled, and other models will have their own. Rather than maintain a
    growing list of per-model regexes, this just extracts the brackets the
    prompt actually asked for and ignores whatever preamble surrounds them."""
    start = text.find("[")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def build_moment_prompt(transcript_segments, description, target_duration=60) -> str:
    """Builds a prompt for finding ONE specific moment the user described,
    instead of ranking the best N clips."""
    lines = [
        f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text'].strip()}"
        for seg in transcript_segments
    ]
    transcript_text = "\n".join(lines)

    return f"""You are finding one specific moment in a video transcript, to cut out as a short clip.

Transcript (timestamps in seconds):
{transcript_text}

The person is looking for this specific moment: "{description}"

Find the single segment in the transcript that best matches what they described. The clip should:
- Run roughly {target_duration} seconds, expanded around the matching moment so it's a complete, self-contained thought
- Start and end at natural sentence/pause boundaries near the described moment, not mid-word

Respond with ONLY a JSON array containing exactly ONE entry, no other text, no markdown fences, in this exact format:
[
  {{"start": 12.4, "end": 73.1, "reason": "why this is the moment they described"}}
]"""


def _ask_for_clips(prompt, model, base_url, think, timeout, attempts=2):
    """Runs the prompt and parses the JSON answer, retrying once if the model
    produced malformed JSON -- small models flub the format occasionally, and
    a fresh sample is usually all it takes."""
    last_err = None
    for attempt in range(1, attempts + 1):
        raw = call_local_llm(prompt, model, base_url, timeout=timeout, think=think)
        cleaned = _strip_thinking_block(raw)
        cleaned = _strip_markdown_fence(cleaned)
        cleaned = _extract_json_array(cleaned)
        try:
            clips = json.loads(cleaned)
        except json.JSONDecodeError as e:
            last_err = RuntimeError(
                f"Model didn't return valid JSON. Raw output was:\n{raw}")
            last_err.__cause__ = e
        else:
            if isinstance(clips, list):
                return clips
            last_err = RuntimeError(f"Expected a JSON array of clips, got: {type(clips)}")
        if attempt < attempts:
            print(f"  (the model's answer wasn't valid JSON -- asking again, "
                  f"attempt {attempt + 1}/{attempts})")
    raise last_err


def select_moment(transcript_segments, description, model="gemma4:12b",
                   target_duration=60, base_url="http://localhost:11434", think=False,
                   timeout=DEFAULT_TIMEOUT):
    """Returns a single-item list with the {start, end, reason} dict for the
    one moment the person described, instead of a ranked list of N clips."""
    if is_thinking_model(model):
        think = True  # R1 models emit nothing useful without thinking
    prompt = build_moment_prompt(transcript_segments, description, target_duration)
    clips = _ask_for_clips(prompt, model, base_url, think, timeout)

    if len(clips) == 0:
        raise RuntimeError("Model didn't find a matching moment.")

    return clips[:1]


def select_highlights(transcript_segments, model="gemma4:12b",
                       target_duration=60, num_clips=3,
                       base_url="http://localhost:11434", think=False,
                       timeout=DEFAULT_TIMEOUT):
    """Returns a list of {start, end, reason} dicts picked by the local model."""
    if is_thinking_model(model):
        think = True  # R1 models emit nothing useful without thinking
    prompt = build_prompt(transcript_segments, target_duration, num_clips)
    clips = _ask_for_clips(prompt, model, base_url, think, timeout)

    # Models occasionally return more entries than asked for -- since the
    # prompt asks for them ranked best-first, keep the top num_clips.
    return clips[:num_clips]


def main():
    parser = argparse.ArgumentParser(
        description="Pick highlight clips from a transcript using a local LLM (no API key needed)."
    )
    parser.add_argument(
        "transcript_json",
        help="Path to a JSON file: list of {start, end, text} segments (Whisper/WhisperX output format)",
    )
    parser.add_argument("--model", default="gemma4:12b",
                         help="Ollama model tag (default: gemma4:12b)")
    parser.add_argument("--duration", type=int, default=60,
                         help="Target clip duration in seconds (default: 60)")
    parser.add_argument("--clips", type=int, default=3,
                         help="Number of highlight clips to pick (default: 3)")
    parser.add_argument("--moment", default=None,
                         help="Instead of ranking the best N clips, find one specific moment "
                              "you describe, e.g. --moment \"the part where they argue about the budget\"")
    parser.add_argument("--base-url", default="http://localhost:11434",
                         help="Ollama's native API base (default: Ollama's default)")
    parser.add_argument("--think", action="store_true",
                         help="Let the model think before answering (slower; off by default)")
    args = parser.parse_args()

    with open(args.transcript_json) as f:
        segments = json.load(f)

    if args.moment:
        clips = select_moment(
            segments,
            args.moment,
            model=args.model,
            target_duration=args.duration,
            base_url=args.base_url,
            think=args.think,
        )
    else:
        clips = select_highlights(
            segments,
            model=args.model,
            target_duration=args.duration,
            num_clips=args.clips,
            base_url=args.base_url,
            think=args.think,
        )
    print(json.dumps(clips, indent=2))


if __name__ == "__main__":
    main()
