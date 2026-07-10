#!/usr/bin/env bash
# shorts -- launcher for 2d.shorts (installed to ~/.local/bin/shorts)
#
#   shorts                  interactive: choose Web UI or CLI
#   shorts <url|file> ...   direct CLI: passes everything to make_shorts.py

set -u

BASE="$HOME/.2dshorts"
APP="$BASE/app"
PY="$BASE/venv/bin/python3"

if [ ! -x "$PY" ] || [ ! -f "$APP/make_shorts.py" ]; then
  echo "2d.shorts doesn't look installed. Run the installer first:"
  echo "  curl -fsSL https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.sh | bash"
  exit 1
fi

if [ -t 1 ]; then
  RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YLW=$'\033[1;33m'; CYN=$'\033[1;36m'
  DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YLW=""; CYN=""; DIM=""; BLD=""; RST=""
fi

# Any argument -> straight to the CLI, no questions asked
if [ "$#" -gt 0 ]; then
  exec "$PY" "$APP/make_shorts.py" "$@"
fi

printf '%s' "$RED"
cat <<'EOF'

  ██████╗ ██████╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ████████╗███████╗
  ╚════██╗██╔══██╗    ██╔════╝██║  ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝
   █████╔╝██║  ██║    ███████╗███████║██║   ██║██████╔╝   ██║   ███████╗
  ██╔═══╝ ██║  ██║    ╚════██║██╔══██║██║   ██║██╔══██╗   ██║   ╚════██║
  ███████╗██████╔╝ ██╗███████║██║  ██║╚██████╔╝██║  ██║   ██║   ███████║
  ╚══════╝╚═════╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝
EOF
printf '%s\n' "$RST"

echo "${BLD}Where do you want to run it?${RST}"
echo "  ${CYN}1)${RST} Web  ${DIM}-- point-and-click in your browser (recommended)${RST}"
echo "  ${CYN}2)${RST} CLI  ${DIM}-- terminal commands and flags${RST}"
read -r -p "Choose [1/2] (default 1): " CHOICE

if [ "${CHOICE:-1}" = "2" ]; then
  echo
  echo "${BLD}CLI guide -- the command is just 'shorts' plus flags:${RST}"
  echo
  echo "  ${GRN}shorts \"https://youtube.com/watch?v=...\" --num-clips 3${RST}"
  echo "      ${DIM}download, pick the 3 best moments, make 3 captioned vertical shorts${RST}"
  echo
  echo "  ${GRN}shorts video.mp4 --num-clips 5${RST}"
  echo "      ${DIM}same but from a local file${RST}"
  echo
  echo "  ${GRN}shorts video.mp4 -m \"the part where they argue about the budget\"${RST}"
  echo "      ${DIM}cut ONE specific moment you describe instead of auto-ranking${RST}"
  echo
  echo "${BLD}The flags that matter:${RST}"
  echo "  --num-clips N            how many shorts to make"
  echo "  -m \"...\"                 find one specific moment instead"
  echo "  --clip-duration 60       target length of each short, in seconds"
  echo "  --reframe-mode MODE      hybrid (default) | smart_crop | blur_letterbox"
  echo "  --caption-style STYLE    animated (default, word-pop) | simple"
  echo "  --words-per-caption N    words shown per caption line (default 5)"
  echo "  --output-dir DIR         where the finished shorts land (default ./shorts_output)"
  echo "  --llm-model TAG          override the Ollama model picked at install"
  echo "  --cookies FILE           cookies.txt if YouTube wants you logged in"
  echo "  --keep-temp              keep working files for debugging"
  echo
  echo "${BLD}Cleaning up stored jobs (the ones the web UI lists):${RST}"
  echo "  shorts --list-jobs       list stored jobs with their ids and sizes"
  echo "  shorts --clear-job ID    delete one job and its clips"
  echo "  shorts --clear-jobs      delete ALL stored jobs"
  echo
  echo "  ${DIM}Full list: shorts --help${RST}"
  echo
  echo "${DIM}Made by 2d.jack -- https://github.com/2d-jack${RST}"
  exit 0
fi

PORT="${PORT:-8080}"
IP=""
case "$(uname -s)" in
  Darwin) IP=$(ipconfig getifaddr en0 2>/dev/null || true) ;;
  *)      IP=$(hostname -I 2>/dev/null | awk '{print $1}') ;;
esac

echo
echo "${YLW}⚠ Heads up about the port:${RST} the web UI listens on port ${BLD}$PORT${RST}."
echo "  If something else already uses it, stop that first or run:  PORT=9000 shorts"
echo "  Anyone on your network can reach this page -- there's no login."
echo
echo "${GRN}Starting the web UI...${RST}"
echo "  Open:        ${BLD}http://localhost:$PORT${RST}"
[ -n "$IP" ] && echo "  Other devices: ${BLD}http://$IP:$PORT${RST}"
echo "  Stop it with Ctrl-C."
echo
echo "${DIM}Made by 2d.jack -- https://github.com/2d-jack${RST}"
echo
exec "$PY" "$APP/webui.py"
