# shorts -- launcher for 2d.shorts (Windows)
#   shorts                  interactive: choose Web UI or CLI
#   shorts <url|file> ...   direct CLI: passes everything to make_shorts.py

$Base = Join-Path $env:USERPROFILE ".2dshorts"
$App  = Join-Path $Base "app"
$Py   = Join-Path $Base "venv\Scripts\python.exe"

if (-not (Test-Path $Py) -or -not (Test-Path (Join-Path $App "make_shorts.py"))) {
  Write-Host "2d.shorts doesn't look installed. Run the installer first:"
  Write-Host "  irm https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.ps1 | iex"
  exit 1
}

# Any argument -> straight to the CLI, no questions asked
if ($args.Count -gt 0) {
  & $Py (Join-Path $App "make_shorts.py") @args
  exit $LASTEXITCODE
}

Write-Host ""
Write-Host "  ██████╗ ██████╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ████████╗███████╗" -ForegroundColor Red
Write-Host "  ╚════██╗██╔══██╗    ██╔════╝██║  ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝" -ForegroundColor Red
Write-Host "   █████╔╝██║  ██║    ███████╗███████║██║   ██║██████╔╝   ██║   ███████╗" -ForegroundColor Red
Write-Host "  ██╔═══╝ ██║  ██║    ╚════██║██╔══██║██║   ██║██╔══██╗   ██║   ╚════██║" -ForegroundColor Red
Write-Host "  ███████╗██████╔╝ ██╗███████║██║  ██║╚██████╔╝██║  ██║   ██║   ███████║" -ForegroundColor Red
Write-Host "  ╚══════╝╚═════╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝" -ForegroundColor Red
Write-Host ""
Write-Host "Where do you want to run it?"
Write-Host "  1) Web  -- point-and-click in your browser (recommended)"
Write-Host "  2) CLI  -- terminal commands and flags"
$choice = Read-Host "Choose [1/2] (default 1)"

if ($choice -eq "2") {
  Write-Host ""
  Write-Host "CLI guide -- the command is just 'shorts' plus flags:" -ForegroundColor White
  Write-Host ""
  Write-Host '  shorts "https://youtube.com/watch?v=..." --num-clips 3' -ForegroundColor Green
  Write-Host "      download, pick the 3 best moments, make 3 captioned vertical shorts" -ForegroundColor DarkGray
  Write-Host ""
  Write-Host "  shorts video.mp4 --num-clips 5" -ForegroundColor Green
  Write-Host "      same but from a local file" -ForegroundColor DarkGray
  Write-Host ""
  Write-Host '  shorts video.mp4 -m "the part where they argue about the budget"' -ForegroundColor Green
  Write-Host "      cut ONE specific moment you describe instead of auto-ranking" -ForegroundColor DarkGray
  Write-Host ""
  Write-Host "The flags that matter:"
  Write-Host "  --num-clips N            how many shorts to make"
  Write-Host '  -m "..."                 find one specific moment instead'
  Write-Host "  --clip-duration 60       target length of each short, in seconds"
  Write-Host "  --reframe-mode MODE      hybrid (default) | smart_crop | blur_letterbox"
  Write-Host "  --caption-style STYLE    animated (default, word-pop) | simple"
  Write-Host "  --words-per-caption N    words shown per caption line (default 5)"
  Write-Host "  --output-dir DIR         where the finished shorts land (default .\shorts_output)"
  Write-Host "  --llm-model TAG          override the Ollama model picked at install"
  Write-Host "  --cookies FILE           cookies.txt if YouTube wants you logged in"
  Write-Host "  --keep-temp              keep working files for debugging"
  Write-Host ""
  Write-Host "Cleaning up stored jobs (the ones the web UI lists):"
  Write-Host "  shorts --list-jobs       list stored jobs with their ids and sizes"
  Write-Host "  shorts --clear-job ID    delete one job and its clips"
  Write-Host "  shorts --clear-jobs      delete ALL stored jobs"
  Write-Host ""
  Write-Host "  Full list: shorts --help" -ForegroundColor DarkGray
  Write-Host ""
  Write-Host "Made by 2d.jack -- https://github.com/2d-jack" -ForegroundColor DarkGray
  exit 0
}

$port = if ($env:PORT) { $env:PORT } else { "8080" }
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
       Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "WARNING about the port: the web UI listens on port $port." -ForegroundColor Yellow
Write-Host "  If something else already uses it, stop that first or run:  `$env:PORT=9000; shorts"
Write-Host "  Anyone on your network can reach this page -- there's no login."
Write-Host ""
Write-Host "Starting the web UI..." -ForegroundColor Green
Write-Host "  Open:          http://localhost:$port"
if ($ip) { Write-Host "  Other devices: http://${ip}:$port" }
Write-Host "  Stop it with Ctrl-C."
Write-Host ""
Write-Host "Made by 2d.jack -- https://github.com/2d-jack" -ForegroundColor DarkGray
Write-Host ""
& $Py (Join-Path $App "webui.py")
