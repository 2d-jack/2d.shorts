#!/usr/bin/env python3
"""
whispercpp.py

GPU transcription for machines WITHOUT an NVIDIA card, via whisper.cpp's
Vulkan backend. faster-whisper (CTranslate2) only accelerates on CUDA, so on
AMD/Intel GPUs -- an RX 550, an Arc card, an iGPU -- this module runs the
same Whisper models through a whisper.cpp binary instead, which talks to any
Vulkan-capable GPU.

Where the binary comes from: our own GitHub release (built from official
ggml-org/whisper.cpp source by .github/workflows/build-whispercpp-vulkan.yml
-- upstream ships no Vulkan builds, and we refuse to auto-download community
binaries). A whisper-cli already on PATH (e.g. `brew install whisper-cpp` on
macOS, which brings Metal GPU support) is used in preference to downloading.

Models are the official ggml conversions from Hugging Face
(huggingface.co/ggerganov/whisper.cpp), cached in the app's data dir.

Output is converted to the exact segment/word format transcribe.py produces,
so captions.py and highlight_selector.py don't know the difference.

Everything here raises on failure -- the caller (make_shorts.py) catches and
falls back to CPU transcription so a broken driver never kills a job.
"""

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

from paths import data_dir

RELEASE_TAG = "whispercpp-v1.9.1"
RELEASE_BASE = f"https://github.com/2d-jack/2d.shorts/releases/download/{RELEASE_TAG}"
ASSETS = {
    ("Windows", "AMD64"): ("whispercpp-vulkan-win-x64.zip", "whisper-cli.exe"),
    ("Linux", "x86_64"): ("whispercpp-vulkan-linux-x64.tar.gz", "whisper-cli"),
}
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{size}.bin"
# Rough download sizes so the first-run log can set expectations
MODEL_MB = {"tiny": 75, "base": 142, "small": 466, "medium": 1500, "large-v3": 2900}


def _home() -> str:
    d = os.path.join(data_dir(), "whispercpp")
    os.makedirs(d, exist_ok=True)
    return d


def _platform_asset():
    return ASSETS.get((platform.system(), platform.machine()))


def find_binary() -> str:
    """An existing whisper-cli, without downloading: our data dir first,
    then PATH (covers brew installs and people who built their own).
    Returns "" if none."""
    asset = _platform_asset()
    exe_name = asset[1] if asset else "whisper-cli"
    local = os.path.join(_home(), exe_name)
    if os.path.isfile(local):
        return local
    return shutil.which("whisper-cli") or ""


def _download(url: str, dest: str, label: str):
    print(f"  downloading {label} ...")
    tmp = dest + ".part"
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last_pct = 0, -10
        while True:
            chunk = resp.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct >= last_pct + 10:
                    print(f"    {pct}% of {total / 1048576:.0f} MB")
                    last_pct = pct
    os.replace(tmp, dest)


def ensure_binary() -> str:
    """Returns a path to whisper-cli, downloading our release build if this
    platform has one and nothing is installed yet. Raises if unavailable."""
    found = find_binary()
    if found:
        return found
    asset = _platform_asset()
    if not asset:
        raise RuntimeError(f"no prebuilt whisper.cpp Vulkan binary for "
                           f"{platform.system()}/{platform.machine()}")
    asset_name, exe_name = asset
    home = _home()
    archive = os.path.join(home, asset_name)
    _download(f"{RELEASE_BASE}/{asset_name}", archive, f"whisper.cpp Vulkan engine ({asset_name})")
    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(home)
    else:
        with tarfile.open(archive) as t:
            t.extractall(home)
    os.remove(archive)
    exe = os.path.join(home, exe_name)
    if not os.path.isfile(exe):
        # archive layout changed? find it anywhere in the extracted tree
        for root, _dirs, files in os.walk(home):
            if exe_name in files:
                exe = os.path.join(root, exe_name)
                break
    if not os.path.isfile(exe):
        raise RuntimeError(f"{exe_name} missing from downloaded archive")
    if os.name != "nt":
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def ensure_model(size: str) -> str:
    """Downloads the official ggml model file on first use."""
    models = os.path.join(_home(), "models")
    os.makedirs(models, exist_ok=True)
    path = os.path.join(models, f"ggml-{size}.bin")
    if not os.path.isfile(path):
        mb = MODEL_MB.get(size)
        label = f"whisper {size} model" + (f" (~{mb} MB, one-time)" if mb else "")
        _download(MODEL_URL.format(size=size), path, label)
    return path


def has_gpu() -> bool:
    """Cheap 'is there any display adapter Vulkan could use' heuristic.
    Deliberately optimistic -- a wrong yes costs one failed attempt followed
    by the automatic CPU fallback, a wrong no costs the user their GPU."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=30).stdout
            names = [n.strip() for n in out.splitlines() if n.strip()]
            return any("basic display" not in n.lower() for n in names)
        if sys.platform == "darwin":
            return True  # Apple GPUs: brew's whisper-cli uses Metal
        return os.path.isdir("/dev/dri") and bool(os.listdir("/dev/dri"))
    except Exception:
        return False


def usable() -> bool:
    """Should the auto engine ladder even try the vulkan rung here?"""
    if not has_gpu():
        return False
    return bool(find_binary() or _platform_asset())


_SPECIAL_TOKEN = ("[_",)  # whisper.cpp emits control tokens like [_BEG_], [_TT_42]


def _tokens_to_words(tokens):
    """Groups whisper.cpp subword tokens into caption-ready words. A token
    whose text starts with a space starts a new word; everything else glues
    onto the previous one (word pieces, punctuation)."""
    words = []
    for tok in tokens:
        text = tok.get("text", "")
        if not text or text.startswith(_SPECIAL_TOKEN):
            continue
        off = tok.get("offsets") or {}
        start = off.get("from", 0) / 1000.0
        end = off.get("to", 0) / 1000.0
        if text.startswith(" ") or not words:
            words.append({"start": round(start, 2), "end": round(end, 2),
                          "word": text.strip()})
        else:
            words[-1]["word"] += text.strip()
            words[-1]["end"] = round(end, 2)
    return [w for w in words if w["word"]]


def transcribe(audio_path: str, model_size: str = "small") -> list:
    """Transcribes a 16 kHz mono wav via whisper.cpp (Vulkan when the GPU
    cooperates). Returns transcribe.py-compatible segments. Raises on any
    failure so the caller can fall back to CPU."""
    exe = ensure_binary()
    model = ensure_model(model_size)
    out_base = audio_path + ".wcpp"

    cmd = [exe, "-m", model, "-f", audio_path,
           "-ojf", "-of", out_base, "-l", "auto",
           "-t", str(os.cpu_count() or 4)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError("whisper.cpp exited with code "
                           f"{proc.returncode}: {' | '.join(tail)}")

    # whisper.cpp prints its compute device to stderr; surface it so the log
    # shows whether the GPU was actually used
    for line in (proc.stderr or "").splitlines():
        if "ggml_vulkan" in line and "device" in line.lower():
            print(f"    ({line.strip()})")
            break

    json_path = out_base + ".json"
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass

    segments = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        off = seg.get("offsets") or {}
        segments.append({
            "start": round(off.get("from", 0) / 1000.0, 2),
            "end": round(off.get("to", 0) / 1000.0, 2),
            "text": text,
            "words": _tokens_to_words(seg.get("tokens") or []),
        })
    if not segments:
        raise RuntimeError("whisper.cpp produced no transcription segments")
    return segments
