# 2d.shorts

Turn any YouTube video (or local file) into **ranked, captioned, vertical shorts** — the OpusClip/Wayin job, running **100% on your own machine**. No API keys, no per-clip cost, your videos never leave your computer.

- **Whisper** (word-level timestamps) transcribes locally — NVIDIA, AMD, Intel GPU, or CPU
- A **local LLM** (Ollama) picks the most clip-worthy moments — or finds the one moment you describe
- **Face-tracked smart cropping** reframes to 9:16, per scene: crops into talking heads, blur-letterboxes B-roll/gameplay
- **Word-pop animated captions** burned in with ffmpeg

## Install (one command)

**Linux / macOS**

```bash
curl -fsSL https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.sh | bash
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.ps1 | iex
```

The installer will:

1. Ask if you want **automatic** install or **step-by-step** (see & confirm every command)
2. Scan your **GPU + RAM** (NVIDIA on Linux/Windows, Apple Silicon on Mac — no GPU, no party 🍿)
3. Let you pick the AI model that fits your RAM:

   | Size | Model | Best for | Min RAM | Download |
   |------|-------|----------|---------|----------|
   | small | `qwen3.5:9b` | low-end devices | 8 GB | ~6 GB |
   | small | `gemma4:12b` | best overall *(suggested)* | 16 GB | ~8 GB |
   | small | `deepseek-r1:8b` | good with thinking, slower output | 8 GB | ~5 GB |
   | small | `deepseek-r1:1.5b` | potato PCs | 4 GB | ~1 GB |
   | medium | `qwen3.6:27b` | strong machines | 32 GB | ~18 GB |
   | big | `deepseek-r1:70b` | workstations | 64 GB | ~43 GB |

   DeepSeek-R1 models need "thinking" to answer — the app switches that on for them automatically.

4. Install everything (ffmpeg, Python env, Ollama, the model) and give you one new command: **`shorts`**

## Use

```
shorts
```

It asks: **Web** (browser UI on port 8080, point-and-click, live logs, previews) or **CLI**. Or skip the menu entirely:

```bash
shorts "https://youtube.com/watch?v=..." --num-clips 3
shorts video.mp4 -m "the part where they argue about the budget"
shorts --help
```

If YouTube wants you logged in, export your browser cookies (any "cookies.txt" browser extension) to the path the installer prints at the end.

## Managing jobs

Every run started from the web UI is stored as a **job** (its log + finished clips) in the app's data folder, so your history survives restarts. When you want to clean up:

**In the web UI**

- Click the **✕** in the corner of any job card, or open a job and click **delete job** — either way you'll be asked to confirm.
- Deleting a **running** job cancels it first (the download/render is killed), then removes its files.

**In the CLI**

```bash
shorts --list-jobs                        # list stored jobs: id, clip count, size on disk
shorts --clear-job 20260709_114352_93fb44 # delete one job (id from --list-jobs or the web UI)
shorts --clear-jobs                       # delete ALL stored jobs
```

`--clear-job` can be repeated to delete several at once. Note this manages the shared job history — clips a CLI run wrote to a custom `--output-dir` live wherever you pointed them.

## How transcription picks its hardware

Every job walks this ladder automatically — no settings needed:

1. **NVIDIA GPU** with working CUDA → faster-whisper on the GPU (fastest, big model)
2. **Any other GPU** — AMD (even old cards like an RX 550), Intel Arc/iGPU → **whisper.cpp with Vulkan**; the engine binary (built from official whisper.cpp source by this repo's CI) and model download automatically on first use
3. **No usable GPU** → faster-whisper on the CPU, using every core

If the Vulkan step fails at runtime for any reason (odd driver, out of VRAM), the job doesn't die — it logs why and continues on the CPU. Force a specific rung with `--whisper-engine cuda|vulkan|cpu`. On macOS, `brew install whisper-cpp` is picked up automatically and brings Metal GPU support.

## Ollama memory

After every job — finished **or** failed — the app tells Ollama to unload the LLM from RAM/VRAM immediately, so nothing sits on your memory between jobs. If you want the Ollama background process fully stopped after each job too, pass `--stop-ollama` or set `"stop_ollama_after_job": true` in the data folder's `config.json` (note: the next job will reload the model from disk, which is slower to start).

If a job fails with "the local LLM didn't answer within N seconds", the model is too big for your machine — switch to `qwen3.5:9b` or `deepseek-r1:1.5b` (`ollama pull` it, then set it in the web UI's Advanced box), or raise the wait with `--llm-timeout` / the web UI's "LLM timeout" field.

## Requirements

- A GPU helps a lot (NVIDIA is fastest, AMD/Intel work via Vulkan) but any 64-bit machine works — the pipeline falls back to CPU
- 8 GB+ RAM with the low-end models, 16 GB+ recommended (more for the bigger models)
- ~15 GB free disk (Python deps + your chosen model)

---

Made with ♥ by **[2d.jack](https://github.com/2d-jack)**
