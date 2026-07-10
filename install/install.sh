#!/usr/bin/env bash
# 2d.shorts installer -- Linux & macOS
#
#   curl -fsSL https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.sh | bash
#
# Env overrides (power users / CI):
#   SHORTS_AUTO=1          skip the mode question, run automatic
#   SHORTS_MODEL=small|medium|big   skip the model question
#   SHORTS_SKIP_MODELS=1   don't ollama-pull the model (just write config)
#   SHORTS_IGNORE_GPU=1    install anyway on a machine with no GPU

set -u

REPO_URL="https://github.com/2d-jack/2d.shorts.git"
BASE="$HOME/.2dshorts"
APP="$BASE/app"
VENV="$BASE/venv"
BIN="$HOME/.local/bin"
LOG="${TMPDIR:-/tmp}/2dshorts-install.log"

# ---------- pretty ----------
if [ -t 1 ]; then
  RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YLW=$'\033[1;33m'; CYN=$'\033[1;36m'
  DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YLW=""; CYN=""; DIM=""; BLD=""; RST=""
fi

banner() {
  printf '%s' "$RED"
  cat <<'EOF'

  ██████╗ ██████╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ████████╗███████╗
  ╚════██╗██╔══██╗    ██╔════╝██║  ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝
   █████╔╝██║  ██║    ███████╗███████║██║   ██║██████╔╝   ██║   ███████╗
  ██╔═══╝ ██║  ██║    ╚════██║██╔══██║██║   ██║██╔══██╗   ██║   ╚════██║
  ███████╗██████╔╝ ██╗███████║██║  ██║╚██████╔╝██║  ██║   ██║   ███████║
  ╚══════╝╚═════╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝
EOF
  printf '%s' "$RST"
  echo "        ${DIM}YouTube video in  →  ranked, captioned vertical shorts out${RST}"
  echo "        ${DIM}100% local. Your GPU, your videos, no API keys.${RST}"
  echo
}

say()  { echo "${CYN}==>${RST} ${BLD}$*${RST}"; }
ok()   { echo "  ${GRN}✓${RST} $*"; }
warn() { echo "  ${YLW}!${RST} $*"; }
die()  { echo; echo "${RED}✗ $*${RST}"; echo "${DIM}Full log: $LOG${RST}"; exit 1; }

# stdin is the pipe when run via `curl | bash` -- prompts must use the terminal
ask() { # ask <varname> <prompt>
  local __var="$1"; shift
  local __ans=""
  if [ -r /dev/tty ]; then read -r -p "$*" __ans < /dev/tty; else read -r -p "$*" __ans; fi
  printf -v "$__var" '%s' "$__ans"
}

# ---------- command runner (the auto/step-mode core) ----------
MODE="auto"
SPIN_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

run() { # run "<description>" <cmd...>
  local desc="$1"; shift
  if [ "$MODE" = "step" ]; then
    echo
    echo "${YLW}NEXT:${RST} $desc"
    echo "  ${BLD}\$ $*${RST}"
    local _junk; ask _junk "  ${DIM}press Enter to run it...${RST}"
    "$@" || die "Command failed: $*"
    ok "$desc"
  else
    # Run in the background and spin a little braille loader on top of it,
    # so long steps (downloads, pip installs, model pulls) don't look frozen.
    "$@" >>"$LOG" 2>&1 &
    local pid=$!
    local i=0 start=$SECONDS
    local nframes=${#SPIN_FRAMES[@]}
    while kill -0 "$pid" 2>/dev/null; do
      local frame="${SPIN_FRAMES[$((i % nframes))]}"
      local elapsed=$(( SECONDS - start ))
      printf '\r  %s %s... (%ss)   ' "$frame" "$desc" "$elapsed"
      i=$((i + 1))
      sleep 0.1
    done
    wait "$pid"
    local status=$?
    printf '\r%*s\r' 90 ''
    if [ "$status" -eq 0 ]; then
      ok "$desc"
    else
      echo "${RED}✗ $desc failed.${RST} Last lines of the log:"
      tail -15 "$LOG" | sed 's/^/    /'
      die "Install aborted"
    fi
  fi
}

# ============================================================ start
banner
: >"$LOG"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="mac" ;;
  *) die "Unsupported OS: $OS (use install.ps1 on Windows)" ;;
esac

# ---------- 1. install mode ----------
if [ "${SHORTS_AUTO:-}" = "1" ]; then
  MODE="auto"
else
  echo "${BLD}How do you want to install?${RST}"
  echo "  ${CYN}1)${RST} Automatic   ${DIM}-- sit back, watch checkmarks appear${RST}"
  echo "  ${CYN}2)${RST} Step-by-step ${DIM}-- see every command and press Enter before it runs${RST}"
  ask REPLY "Choose [1/2] (default 1): "
  case "$REPLY" in 2) MODE="step" ;; *) MODE="auto" ;; esac
fi
echo

# ---------- 2. hardware scan ----------
warn "Heads up: I'm about to scan your hardware (GPU + RAM) to make sure"
warn "this machine can actually run local AI video processing."
echo

# Any real GPU counts (NVIDIA / AMD / Intel, discrete or integrated).
GPU_DESC=""
if [ "$PLATFORM" = "mac" ]; then
  if [ "$ARCH" = "arm64" ]; then
    GPU_DESC="Apple Silicon ($(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo arm64))"
  else
    GPU_DESC="$(system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/ {print $2; exit}')"
  fi
else
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    GPU_DESC="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
  elif command -v lspci >/dev/null 2>&1; then
    GPU_DESC="$(lspci | grep -Ei 'vga compatible|3d controller|display controller' | \
                 grep -Ei 'nvidia|amd|ati|radeon|intel' | head -1 | \
                 sed -E 's/^[0-9a-f:.]+ [^:]+: //')"
  fi
fi

if [ -z "$GPU_DESC" ] && [ "${SHORTS_IGNORE_GPU:-}" != "1" ]; then
  echo "${RED}"
  echo "  ✗ No GPU found."
  echo "${RST}"
  echo "  this ain't bussin, your VRAM is NOT enough for this fit 💀"
  echo
  echo "  ${DIM}(know what you're doing? re-run with SHORTS_IGNORE_GPU=1)${RST}"
  exit 1
fi
ok "GPU: ${GPU_DESC:-check skipped (SHORTS_IGNORE_GPU=1)}"

if [ "$PLATFORM" = "mac" ]; then
  RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
else
  RAM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 ))
fi
ok "RAM: ${RAM_GB} GB"
echo

# ---------- 3. AI model choice ----------
pick_default() {
  if   [ "$RAM_GB" -ge 64 ]; then echo 6
  elif [ "$RAM_GB" -ge 32 ]; then echo 5
  elif [ "$RAM_GB" -ge 16 ]; then echo 2
  elif [ "$RAM_GB" -lt 8  ]; then echo 4
  else echo 1; fi
}

case "${SHORTS_MODEL:-}" in
  lowend) CHOICE=1 ;;
  small)  CHOICE=2 ;;   # kept for backward compat: "small" was gemma4:12b
  r1)     CHOICE=3 ;;
  potato) CHOICE=4 ;;
  medium) CHOICE=5 ;;
  big)    CHOICE=6 ;;
  *)
    SUGGEST=$(pick_default)
    echo "${BLD}Which AI model should pick your highlights?${RST} ${DIM}(runs locally via Ollama)${RST}"
    echo "  ${BLD}Small (pick one of these on a normal PC):${RST}"
    echo "  ${CYN}1)${RST} qwen3.5:9b       ${DIM}best for low-end devices        needs  8+ GB RAM, ~6 GB download${RST}"
    echo "  ${CYN}2)${RST} gemma4:12b       ${DIM}best overall for this           needs 16+ GB RAM, ~8 GB download${RST}"
    echo "  ${CYN}3)${RST} deepseek-r1:8b   ${DIM}good with thinking, slower      needs  8+ GB RAM, ~5 GB download${RST}"
    echo "  ${CYN}4)${RST} deepseek-r1:1.5b ${DIM}pick if you have a potato PC    needs  4+ GB RAM, ~1 GB download${RST}"
    echo "  ${BLD}Bigger (only with serious RAM):${RST}"
    echo "  ${CYN}5)${RST} medium -- qwen3.6:27b     ${DIM}needs 32+ GB RAM, ~18 GB download${RST}"
    echo "  ${CYN}6)${RST} big    -- deepseek-r1:70b ${DIM}needs 64+ GB RAM, ~43 GB download${RST}"
    echo "  ${DIM}(DeepSeek-R1 models think before answering -- the app enables that automatically.)${RST}"
    ask CHOICE "Choose [1-6] (your ${RAM_GB} GB RAM suggests $SUGGEST): "
    case "$CHOICE" in 1|2|3|4|5|6) : ;; *) CHOICE=$SUGGEST ;; esac
    ;;
esac

case "$CHOICE" in
  1) MODEL="qwen3.5:9b";       NEED_GB=8  ;;
  2) MODEL="gemma4:12b";       NEED_GB=16 ;;
  3) MODEL="deepseek-r1:8b";   NEED_GB=8  ;;
  4) MODEL="deepseek-r1:1.5b"; NEED_GB=4  ;;
  5) MODEL="qwen3.6:27b";      NEED_GB=32 ;;
  6) MODEL="deepseek-r1:70b";  NEED_GB=64 ;;
esac
if [ "$RAM_GB" -lt "$NEED_GB" ]; then
  warn "$MODEL wants ${NEED_GB}+ GB RAM but you have ${RAM_GB} GB -- it may be painfully slow or fail."
  ask REPLY "Continue with $MODEL anyway? [y/N]: "
  case "$REPLY" in y|Y) : ;; *) die "Re-run and pick a smaller model." ;; esac
fi
ok "Model: $MODEL"
echo

# ---------- 4. system packages ----------
say "System packages"
if [ "$PLATFORM" = "mac" ]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew is required on macOS. Install it from https://brew.sh then re-run."
  command -v ffmpeg >/dev/null 2>&1 || run "Install ffmpeg" brew install ffmpeg
  command -v node   >/dev/null 2>&1 || run "Install node (yt-dlp needs it for YouTube)" brew install node
  command -v git    >/dev/null 2>&1 || run "Install git" brew install git
else
  # Only touch the package manager (and ask for sudo) if something is
  # actually missing -- on an already-provisioned box this stage is silent.
  MISSING=""
  for tool in ffmpeg git curl node; do
    command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
  done
  python3 -m venv --help >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
  if [ -n "$MISSING" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      run "Refresh package lists (needs sudo)" sudo apt-get update -y
      run "Install missing packages:$MISSING" sudo apt-get install -y ffmpeg git curl nodejs python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
      run "Install missing packages:$MISSING (needs sudo)" sudo dnf install -y ffmpeg git curl nodejs python3 python3-pip
    else
      warn "Unknown package manager -- make sure ffmpeg, git, node and python3 are installed."
    fi
  else
    ok "ffmpeg / git / node / python already present"
  fi
fi

# ---------- 5. find a Python that mediapipe supports (3.9 - 3.12) ----------
PYBIN=""
for cand in python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)
    case "$v" in 3.9|3.10|3.11|3.12) PYBIN="$cand"; break ;; esac
  fi
done
if [ -z "$PYBIN" ]; then
  if [ "$PLATFORM" = "mac" ]; then
    run "Install Python 3.12 (your python3 is too new/old for mediapipe)" brew install python@3.12
    PYBIN="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  elif command -v apt-get >/dev/null 2>&1; then
    run "Install Python 3.12" sudo apt-get install -y python3.12 python3.12-venv
    PYBIN="python3.12"
  else
    die "Couldn't find Python 3.9-3.12. Install one and re-run."
  fi
fi
ok "Python: $("$PYBIN" --version 2>&1)"

# ---------- 6. Ollama ----------
say "Ollama (local LLM runtime)"
if ! command -v ollama >/dev/null 2>&1; then
  if [ "$PLATFORM" = "mac" ]; then
    run "Install Ollama" brew install ollama
  else
    run "Install Ollama (their official installer, needs sudo)" \
        bash -c "curl -fsSL https://ollama.com/install.sh | sh"
  fi
else
  ok "Ollama already installed"
fi
# make sure the server is up
if ! curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  if [ "$PLATFORM" = "mac" ]; then
    run "Start Ollama service" brew services start ollama
  else
    (sudo systemctl start ollama >>"$LOG" 2>&1) || (nohup ollama serve >>"$LOG" 2>&1 &)
  fi
  for _ in $(seq 1 15); do
    curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

# ---------- 7. the app ----------
say "2d.shorts code"
mkdir -p "$BASE" "$BIN"
if [ -d "$APP/.git" ]; then
  run "Update existing install" git -C "$APP" pull --ff-only
else
  run "Clone the repo" git clone --depth 1 "$REPO_URL" "$APP"
fi
[ -d "$VENV" ] || run "Create Python environment" "$PYBIN" -m venv "$VENV"
# python -m pip (not calling the pip binary directly) is the more robust way
# to self-upgrade pip -- keeps this consistent with the Windows installer.
run "Upgrade pip" "$VENV/bin/python" -m pip install --quiet --upgrade pip
run "Install Python dependencies (this is the big one, be patient)" \
    "$VENV/bin/python" -m pip install --quiet -r "$APP/requirements.txt"

# ---------- 8. pull the model ----------
if [ "${SHORTS_SKIP_MODELS:-}" != "1" ]; then
  say "AI model download ($MODEL) -- grab a coffee, this is gigabytes"
  run "ollama pull $MODEL" ollama pull "$MODEL"
fi

# ---------- 9. per-device config + the `shorts` command ----------
say "Finishing up"
if [ "$PLATFORM" = "mac" ]; then DATA="$HOME/Library/Application Support/2d.shorts";
else DATA="$HOME/.local/share/2d.shorts"; fi
mkdir -p "$DATA"
printf '{\n  "llm_model": "%s",\n  "llm_base_url": "http://localhost:11434"\n}\n' "$MODEL" > "$DATA/config.json"
ok "Config written ($DATA/config.json)"

install -m 755 "$APP/scripts/shorts.sh" "$BIN/shorts"
ok "Installed the 'shorts' command -> $BIN/shorts"

case ":$PATH:" in
  *":$BIN:"*) : ;;
  *)
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
      grep -qs '\.local/bin' "$rc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
    done
    warn "Added ~/.local/bin to your PATH -- open a NEW terminal (or 'source ~/.zshrc') first."
    ;;
esac

# ---------- 10. the guide ----------
echo
echo "${GRN}"
cat <<'EOF'
   ██████████████████████████████████████████
   █                                        █
   █   INSTALLED. YOU'RE READY TO COOK. 🎬  █
   █                                        █
   ██████████████████████████████████████████
EOF
echo "${RST}"
echo "${BLD}How to use it -- one command:${RST}"
echo
echo "    ${CYN}shorts${RST}"
echo
echo "  It will ask whether you want the ${BLD}Web UI${RST} (point-and-click in your"
echo "  browser) or the ${BLD}CLI${RST} (flags & scripting). That's it."
echo
echo "  ${DIM}Quick taste of the CLI:${RST}"
echo "    shorts \"https://youtube.com/watch?v=...\" --num-clips 3"
echo "    shorts video.mp4 -m \"the part where they argue about the budget\""
echo
echo "  ${DIM}YouTube blocking downloads? Export your browser cookies to:${RST}"
echo "    $DATA/cookies.txt"
echo
echo "${DIM}────────────────────────────────────────────────────────────${RST}"
echo "  Made with ♥ by ${BLD}2d.jack${RST} -- https://github.com/2d-jack"
echo "${DIM}────────────────────────────────────────────────────────────${RST}"
