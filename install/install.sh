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
    # </dev/null: under `curl | bash` stdin IS the rest of this script --
    # a command that reads stdin (installers, apt, brew) would swallow it.
    "$@" </dev/null || die "Command failed: $*"
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
  hash -r 2>/dev/null || true   # forget cached command paths -> use what was just installed
}

# ---------- "is it installed? is it the latest?" helpers ----------
have() { command -v "$1" >/dev/null 2>&1; }

# version_lt A B -> true when version A is older than version B
version_lt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

# sudo only when needed, asked for once, up front, in the foreground --
# a password prompt hidden behind the spinner looks like a frozen install.
SUDO=""
need_sudo() {
  [ "$(id -u)" -eq 0 ] && return 0
  [ -n "$SUDO" ] && return 0
  have sudo || die "This step needs root and 'sudo' isn't available. Re-run as root."
  warn "The next step needs admin rights -- sudo may ask for your password."
  if [ -r /dev/tty ]; then sudo -v </dev/tty; else sudo -v; fi || die "sudo failed"
  SUDO="sudo"
}

# brew_ensure <formula> <command or ""> <why>
#   installed by brew + outdated  -> brew upgrade
#   installed by brew + latest    -> skip
#   on PATH but not from brew     -> skip (we can't safely update someone else's install)
#   missing                       -> brew install
brew_ensure() {
  local f="$1" cmd="$2" why="$3" cur
  if cur="$(brew list --formula --versions "$f" 2>/dev/null)" && [ -n "$cur" ]; then
    if [ -n "$(brew outdated --formula --quiet "$f" 2>/dev/null)" ]; then
      run "Update $f ${cur##* } -> latest ($why)" brew upgrade "$f"
      return 10   # "was updated" -- callers that need to restart something check this
    fi
    ok "$f ${cur##* } already installed (latest)"
  elif [ -n "$cmd" ] && have "$cmd"; then
    ok "$cmd found at $(command -v "$cmd") (not from Homebrew -- leaving it as is)"
  else
    run "Install $f ($why)" brew install "$f"
  fi
  return 0
}

# apt_state <pkg> -> prints missing | outdated | ok   (needs a fresh `apt-get update`)
apt_state() {
  local st inst cand
  st="$(dpkg-query -W -f='${Status} ${Version}' "$1" 2>/dev/null)"
  case "$st" in "install ok installed "*) inst="${st##* }" ;; *) echo missing; return ;; esac
  cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
  if [ -n "$cand" ] && [ "$cand" != "(none)" ] && [ "$cand" != "$inst" ]; then echo outdated; else echo ok; fi
}

# dnf_state <pkg> -> prints missing | outdated | ok
dnf_state() {
  rpm -q "$1" >/dev/null 2>&1 || { echo missing; return; }
  dnf -q check-update "$1" >/dev/null 2>&1
  [ $? -eq 100 ] && echo outdated || echo ok
}

# Python version helpers
py_minor() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null; }
py_supported() { case "$1" in 3.9|3.10|3.11|3.12) return 0 ;; *) return 1 ;; esac; }

# Ollama: version of the installed CLI, and of the server that's actually running
ollama_client_version() {
  ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -1
}
ollama_server_version() {
  curl -s --max-time 2 http://localhost:11434/api/version 2>/dev/null \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}
ollama_up() { curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; }
wait_ollama() {
  for _ in $(seq 1 20); do ollama_up && return 0; sleep 1; done
  return 1
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
# Every package: installed? -> latest? -> skip, otherwise install/upgrade.
say "System packages (checking what's installed and whether it's the latest)"
if [ "$PLATFORM" = "mac" ]; then
  have brew || die "Homebrew is required on macOS. Install it from https://brew.sh then re-run."
  # `brew outdated` only knows about versions brew has seen -- refresh first
  # so "latest" really means latest.
  run "Refresh Homebrew's package index" brew update
  brew_ensure git  git  "needed to download the app"
  brew_ensure node node "yt-dlp needs it for YouTube"
  brew_ensure ffmpeg ffmpeg "video processing"
  # Homebrew's current ffmpeg bottle is built without libass, which the
  # caption burner needs. captions.py already looks for the keg-only
  # ffmpeg@7 (which still has it), so make sure one of them can burn captions.
  ffmpeg_has_libass() { "$1" -hide_banner -filters 2>/dev/null | grep -qE '[[:space:]]ass[[:space:]]+V->V'; }
  if ffmpeg_has_libass ffmpeg; then
    ok "ffmpeg can burn captions (libass present)"
  else
    brew_ensure ffmpeg@7 "" "your ffmpeg has no libass -- this one is used for burning captions"
  fi
else
  # Linux: map package -> command, so tools installed some other way
  # (nvm's node, a static ffmpeg, ...) are respected and not double-installed.
  if have apt-get; then
    need_sudo
    run "Refresh package lists" $SUDO apt-get update -y
    TODO=""
    for spec in ffmpeg:ffmpeg git:git curl:curl nodejs:node python3-venv:; do
      pkg="${spec%%:*}"; cmd="${spec#*:}"
      case "$(apt_state "$pkg")" in
        ok)       ok "$pkg already installed (latest)" ;;
        outdated) TODO="$TODO $pkg" ;;
        missing)
          if [ -n "$cmd" ] && have "$cmd"; then ok "$cmd found at $(command -v "$cmd") (not from apt -- leaving it as is)"
          else TODO="$TODO $pkg"; fi ;;
      esac
    done
    if [ -n "$TODO" ]; then
      # `apt-get install` on an installed package upgrades it to the candidate
      run "Install / update:$TODO" $SUDO apt-get install -y $TODO
    fi
  elif have dnf; then
    NEW=""; UPD=""
    for spec in ffmpeg:ffmpeg git:git curl:curl nodejs:node python3:python3; do
      pkg="${spec%%:*}"; cmd="${spec#*:}"
      case "$(dnf_state "$pkg")" in
        ok)       ok "$pkg already installed (latest)" ;;
        outdated) UPD="$UPD $pkg" ;;
        missing)
          if have "$cmd"; then ok "$cmd found at $(command -v "$cmd") (not from dnf -- leaving it as is)"
          else NEW="$NEW $pkg"; fi ;;
      esac
    done
    if [ -n "$NEW$UPD" ]; then need_sudo; fi
    [ -n "$NEW" ] && run "Install:$NEW" $SUDO dnf install -y $NEW
    [ -n "$UPD" ] && run "Update:$UPD" $SUDO dnf upgrade -y $UPD
  else
    MISSING=""
    for tool in ffmpeg git curl node python3; do have "$tool" || MISSING="$MISSING $tool"; done
    if [ -n "$MISSING" ]; then
      die "Unknown package manager and these are missing:$MISSING -- install them, then re-run."
    fi
    warn "Unknown package manager -- found ffmpeg/git/curl/node/python3 but can't check them for updates."
  fi
fi

# ---------- 5. find a Python that mediapipe supports (3.9 - 3.12) ----------
PYBIN=""
for cand in python3.12 python3.11 python3.10 python3.9 python3; do
  if have "$cand" && py_supported "$(py_minor "$cand")"; then PYBIN="$(command -v "$cand")"; break; fi
done
if [ -z "$PYBIN" ]; then
  if [ "$PLATFORM" = "mac" ]; then
    run "Install Python 3.12 (your python3 is too new/old for mediapipe)" brew install python@3.12
    PYBIN="$(brew --prefix)/opt/python@3.12/bin/python3.12"
  elif have apt-get; then
    need_sudo
    run "Install Python 3.12" $SUDO apt-get install -y python3.12 python3.12-venv
    PYBIN="$(command -v python3.12 || true)"
  elif have dnf; then
    need_sudo
    run "Install Python 3.12" $SUDO dnf install -y python3.12
    PYBIN="$(command -v python3.12 || true)"
  fi
  [ -n "$PYBIN" ] || die "Couldn't find or install Python 3.9-3.12. Install one and re-run."
else
  PYV="$(py_minor "$PYBIN")"
  # If this Python came from a package manager, keep it on the latest patch release.
  if [ "$PLATFORM" = "mac" ] && brew list --formula --versions "python@$PYV" >/dev/null 2>&1; then
    brew_ensure "python@$PYV" "" "Python for the app"
  elif have apt-get && [ "$PYBIN" != "$(command -v python3 2>/dev/null)" ]; then
    # python3 itself was covered via python3-venv above; a side-by-side
    # python3.X needs its OWN -venv package or `-m venv` fails (no ensurepip).
    case "$(apt_state "python$PYV-venv")" in
      ok) : ;;
      *) need_sudo; run "Install / update python$PYV-venv" $SUDO apt-get install -y "python$PYV" "python$PYV-venv" ;;
    esac
  fi
fi
ok "Python: $("$PYBIN" --version 2>&1) ($PYBIN)"

# ---------- 6. Ollama ----------
say "Ollama (local LLM runtime)"
OLLAMA_UPDATED=0
if [ "$PLATFORM" = "mac" ]; then
  if brew list --formula --versions ollama >/dev/null 2>&1 || ! have ollama; then
    brew_ensure ollama ollama "runs the AI model locally"
    [ $? -eq 10 ] && OLLAMA_UPDATED=1
  else
    ok "Ollama $(ollama_client_version) found (desktop app -- it keeps itself updated)"
  fi
else
  if ! have ollama; then
    need_sudo
    run "Install Ollama (their official installer, needs sudo)" \
        bash -c "curl -fsSL https://ollama.com/install.sh | sh"
  else
    CUR="$(ollama_client_version)"
    LATEST="$(curl -fsSL --max-time 10 https://api.github.com/repos/ollama/ollama/releases/latest 2>/dev/null \
              | grep -m1 '"tag_name"' | sed -E 's/.*"v?([^"]+)".*/\1/')"
    if [ -z "$LATEST" ] || [ -z "$CUR" ]; then
      warn "Ollama ${CUR:-?} installed -- couldn't check for a newer version, keeping it."
    elif version_lt "$CUR" "$LATEST"; then
      need_sudo
      # the official installer upgrades in place
      run "Update Ollama $CUR -> $LATEST (official installer, needs sudo)" \
          bash -c "curl -fsSL https://ollama.com/install.sh | sh"
      OLLAMA_UPDATED=1
    else
      ok "Ollama $CUR already installed (latest)"
    fi
  fi
fi

start_ollama() {
  if [ "$PLATFORM" = "mac" ] && brew list --formula --versions ollama >/dev/null 2>&1; then
    run "$1 Ollama service" brew services "$2" ollama
  elif [ "$PLATFORM" = "mac" ]; then
    # desktop app install: (re)launch it, it runs the server
    [ "$2" = "restart" ] && { osascript -e 'quit app "Ollama"' >/dev/null 2>&1; sleep 2; }
    open -a Ollama >/dev/null 2>&1 || (nohup ollama serve >>"$LOG" 2>&1 &)
  elif have systemctl && systemctl cat ollama >/dev/null 2>&1; then
    need_sudo
    $SUDO systemctl "$2" ollama >>"$LOG" 2>&1 || (nohup ollama serve >>"$LOG" 2>&1 &)
  else
    [ "$2" = "restart" ] && { pkill -f 'ollama serve' >/dev/null 2>&1; sleep 1; }
    (nohup ollama serve >>"$LOG" 2>&1 &)
  fi
}

# Make sure the server is up AND is the version we just installed -- an
# already-running old server would keep serving the old version otherwise.
if ! ollama_up; then
  start_ollama "Start" start
elif [ "$OLLAMA_UPDATED" = "1" ] || { [ -n "$(ollama_server_version)" ] \
     && [ "$(ollama_server_version)" != "$(ollama_client_version)" ]; }; then
  start_ollama "Restart (to load the new version of)" restart
fi
if wait_ollama; then
  ok "Ollama server running (v$(ollama_server_version))"
else
  warn "Ollama server didn't answer on localhost:11434 yet -- start it with 'ollama serve' if jobs fail."
fi

# ---------- 7. the app ----------
say "2d.shorts code"
mkdir -p "$BASE" "$BIN"
if [ -d "$APP/.git" ]; then
  run "Check for app updates" git -C "$APP" fetch --quiet origin
  if [ "$(git -C "$APP" rev-parse HEAD 2>/dev/null)" = "$(git -C "$APP" rev-parse '@{u}' 2>/dev/null)" ]; then
    ok "App code already up to date ($(git -C "$APP" rev-parse --short HEAD))"
  else
    run "Update app code" git -C "$APP" pull --ff-only
  fi
else
  run "Clone the repo" git clone --depth 1 "$REPO_URL" "$APP"
fi

# Reuse the venv only if it still works and is on a mediapipe-compatible
# Python. A Homebrew/apt Python upgrade can leave an old venv pointing at an
# interpreter that no longer exists -- then rebuild it from scratch.
if [ -d "$VENV" ]; then
  VV="$(py_minor "$VENV/bin/python")"
  if py_supported "$VV" && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    ok "Python environment OK (Python $VV)"
  else
    warn "Existing Python environment is broken or on an unsupported Python (${VV:-unknown}) -- rebuilding it."
    run "Remove broken Python environment" rm -rf "$VENV"
  fi
fi
[ -d "$VENV" ] || run "Create Python environment" "$PYBIN" -m venv "$VENV"

# python -m pip (not calling the pip binary directly) is the more robust way
# to self-upgrade pip -- keeps this consistent with the Windows installer.
if "$VENV/bin/python" -m pip list --outdated --format=freeze 2>/dev/null | grep -qi '^pip=='; then
  run "Update pip" "$VENV/bin/python" -m pip install --quiet --upgrade pip
else
  ok "pip $("$VENV/bin/python" -m pip --version | awk '{print $2}') already latest"
fi

# Ask pip what an upgrade WOULD change (dry run, nothing installed), so
# packages that are already installed at their newest allowed version are
# skipped instead of reinstalled. Version caps in requirements.txt still apply.
say "Python packages (checking installed versions against the latest)"
PIP_PLAN="$("$VENV/bin/python" -m pip install --upgrade --dry-run --quiet --report - \
             -r "$APP/requirements.txt" 2>>"$LOG" | "$VENV/bin/python" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("?"); sys.exit()
print(" ".join("%s==%s" % (i["metadata"]["name"], i["metadata"]["version"]) for i in d.get("install", [])))
' 2>>"$LOG")"
if [ -z "$PIP_PLAN" ]; then
  ok "All Python packages already installed at their latest versions"
else
  [ "$PIP_PLAN" != "?" ] && echo "  ${DIM}to install/update: $PIP_PLAN${RST}"
  run "Install / update Python packages (the big one, be patient)" \
      "$VENV/bin/python" -m pip install --quiet --upgrade -r "$APP/requirements.txt"
fi

# ---------- 8. pull the model ----------
if [ "${SHORTS_SKIP_MODELS:-}" != "1" ]; then
  # `ollama pull` only downloads layers that changed, so on an existing model
  # it's a quick "is there a newer version?" check rather than a re-download.
  if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    say "AI model ($MODEL) is installed -- checking for an updated version"
    run "ollama pull $MODEL (only changed parts are downloaded)" ollama pull "$MODEL"
  else
    say "AI model download ($MODEL) -- grab a coffee, this is gigabytes"
    run "ollama pull $MODEL" ollama pull "$MODEL"
  fi
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

# Put ~/.local/bin on PATH for EVERY shell the user might open -- not just
# the one running this installer. (Old behaviour: if the installer's own
# PATH already had it, e.g. from .zshrc, nothing was written at all, so bash
# never learned about `shorts`. And on macOS, bash reads ~/.bash_profile,
# not ~/.bashrc, for new Terminal windows.)
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
add_path_line() { # add_path_line <rc file>
  local rc="$1"
  if [ -f "$rc" ] && grep -qsE '^[^#]*PATH=.*(\$HOME|~|'"$HOME"')/\.local/bin' "$rc"; then
    return 0   # already there (ours or the user's own)
  fi
  printf '\n# added by the 2d.shorts installer\n%s\n' "$PATH_LINE" >>"$rc" \
    && ok "Added ~/.local/bin to PATH in ~/${rc#"$HOME"/}"
}
USER_SHELL="$(basename "${SHELL:-}")"

# zsh
if [ "$USER_SHELL" = "zsh" ] || [ -f "$HOME/.zshrc" ] || [ "$PLATFORM" = "mac" ]; then
  add_path_line "$HOME/.zshrc"
fi
# bash: interactive non-login shells read ~/.bashrc; login shells (every new
# macOS Terminal window, SSH sessions) read the FIRST of these that exists.
add_path_line "$HOME/.bashrc"
BASH_LOGIN=""
for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
  [ -f "$f" ] && { BASH_LOGIN="$f"; break; }
done
if [ -z "$BASH_LOGIN" ]; then
  if [ "$PLATFORM" = "mac" ]; then BASH_LOGIN="$HOME/.bash_profile"; else BASH_LOGIN="$HOME/.profile"; fi
fi
add_path_line "$BASH_LOGIN"
# fish
if [ "$USER_SHELL" = "fish" ] || [ -d "$HOME/.config/fish" ]; then
  mkdir -p "$HOME/.config/fish/conf.d"
  printf '# added by the 2d.shorts installer\ncontains -- $HOME/.local/bin $PATH; or set -gx PATH $HOME/.local/bin $PATH\n' \
    > "$HOME/.config/fish/conf.d/2dshorts.fish"
  ok "Added ~/.local/bin to PATH for fish"
fi

case ":$PATH:" in
  *":$BIN:"*) : ;;
  *)
    case "$USER_SHELL" in
      zsh)  RELOAD="source ~/.zshrc" ;;
      bash) if [ "$PLATFORM" = "mac" ]; then RELOAD="source ~/${BASH_LOGIN#"$HOME"/}"; else RELOAD="source ~/.bashrc"; fi ;;
      *)    RELOAD="exec \$SHELL -l" ;;
    esac
    warn "Open a NEW terminal (or run: $RELOAD) before using 'shorts'."
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
