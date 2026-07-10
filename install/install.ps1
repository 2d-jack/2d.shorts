# 2d.shorts installer -- Windows (PowerShell)
#
#   irm https://raw.githubusercontent.com/2d-jack/2d.shorts/main/install/install.ps1 | iex
#
# Env overrides:  $env:SHORTS_AUTO=1  $env:SHORTS_MODEL="small"  $env:SHORTS_SKIP_MODELS=1  $env:SHORTS_IGNORE_GPU=1

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/2d-jack/2d.shorts.git"
$Base = Join-Path $env:USERPROFILE ".2dshorts"
$App  = Join-Path $Base "app"
$Venv = Join-Path $Base "venv"
$Bin  = Join-Path $Base "bin"
$Data = Join-Path $env:LOCALAPPDATA "2d.shorts"

function Banner {
  Write-Host ""
  Write-Host "  ██████╗ ██████╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ████████╗███████╗" -ForegroundColor Red
  Write-Host "  ╚════██╗██╔══██╗    ██╔════╝██║  ██║██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝" -ForegroundColor Red
  Write-Host "   █████╔╝██║  ██║    ███████╗███████║██║   ██║██████╔╝   ██║   ███████╗" -ForegroundColor Red
  Write-Host "  ██╔═══╝ ██║  ██║    ╚════██║██╔══██║██║   ██║██╔══██╗   ██║   ╚════██║" -ForegroundColor Red
  Write-Host "  ███████╗██████╔╝ ██╗███████║██║  ██║╚██████╔╝██║  ██║   ██║   ███████║" -ForegroundColor Red
  Write-Host "  ╚══════╝╚═════╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝" -ForegroundColor Red
  Write-Host "        YouTube video in  ->  ranked, captioned vertical shorts out" -ForegroundColor DarkGray
  Write-Host "        100% local. Your GPU, your videos, no API keys." -ForegroundColor DarkGray
  Write-Host ""
}

function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host ""; Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

$script:SpinnerFrames = @('⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏')

$script:Mode = "auto"
function Run($desc, [scriptblock]$cmd) {
  if ($script:Mode -eq "step") {
    Write-Host ""
    Write-Host "NEXT: $desc" -ForegroundColor Yellow
    Write-Host "  > $($cmd.ToString().Trim())" -ForegroundColor White
    Read-Host "  press Enter to run it" | Out-Null
    & $cmd
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { Die "Command failed: $desc" }
    Ok $desc
  } else {
    # Spin a little braille loader on its own runspace while $cmd runs here,
    # so long steps (downloads, pip installs, model pulls) don't look frozen.
    $spinPS = [PowerShell]::Create()
    $spinPS.AddScript({
      param($label, $frames)
      $i = 0
      $sw = [Diagnostics.Stopwatch]::StartNew()
      while ($true) {
        $f = $frames[$i % $frames.Length]
        [Console]::Write("`r  $f $label... ($([math]::Floor($sw.Elapsed.TotalSeconds))s)   ")
        Start-Sleep -Milliseconds 100
        $i++
      }
    }).AddArgument($desc).AddArgument($script:SpinnerFrames) | Out-Null
    $spinPS.BeginInvoke() | Out-Null

    $ok = $true
    try {
      & $cmd | Out-Null
      if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { $ok = $false }
    } catch { $ok = $false }

    try { $spinPS.Stop() } catch {}
    $spinPS.Dispose()
    Write-Host ("`r" + (" " * 90) + "`r") -NoNewline

    if (-not $ok) { Die "$desc failed" }
    Ok $desc
  }
}

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path","User")
}

# ============================================================ start
Banner

# ---------- 1. install mode ----------
if ($env:SHORTS_AUTO -eq "1") { $script:Mode = "auto" }
else {
  Write-Host "How do you want to install?"
  Write-Host "  1) Automatic    -- sit back, watch checkmarks appear"
  Write-Host "  2) Step-by-step -- see every command and press Enter before it runs"
  $r = Read-Host "Choose [1/2] (default 1)"
  if ($r -eq "2") { $script:Mode = "step" }
}
Write-Host ""

# ---------- 2. hardware scan ----------
Warn "Heads up: I'm about to scan your hardware (GPU + RAM) to make sure"
Warn "this machine can actually run local AI video processing."
Write-Host ""

# Any real GPU counts (NVIDIA / AMD / Intel, discrete or integrated).
# Filter out virtual/basic adapters (RDP sessions, some VMs) so they don't
# falsely pass the check.
$allGpus = Get-CimInstance Win32_VideoController | Where-Object {
  $_.Name -notmatch "Basic Display|Basic Render|Remote Desktop|Virtual"
}
$gpu = ($allGpus | Where-Object { $_.Name -match "NVIDIA|AMD|Radeon|Intel" } |
        Select-Object -First 1).Name
if (-not $gpu) { $gpu = ($allGpus | Select-Object -First 1).Name }

if (-not $gpu -and $env:SHORTS_IGNORE_GPU -ne "1") {
  Write-Host ""
  Write-Host "  [X] No GPU found." -ForegroundColor Red
  Write-Host ""
  Write-Host "  this ain't bussin, your VRAM is NOT enough for this fit 💀"
  Write-Host ""
  Write-Host "  (know what you're doing? set `$env:SHORTS_IGNORE_GPU=1 and re-run)" -ForegroundColor DarkGray
  exit 1
}
if ($gpu) { Ok "GPU: $gpu" } else { Ok "GPU: check skipped (SHORTS_IGNORE_GPU=1)" }

$ramGB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Ok "RAM: $ramGB GB"
Write-Host ""

# ---------- 3. AI model choice ----------
$suggest = 1
if ($ramGB -ge 64) { $suggest = 6 }
elseif ($ramGB -ge 32) { $suggest = 5 }
elseif ($ramGB -ge 16) { $suggest = 2 }
elseif ($ramGB -lt 8)  { $suggest = 4 }

switch ($env:SHORTS_MODEL) {
  "lowend" { $choice = 1 }
  "small"  { $choice = 2 }   # kept for backward compat: "small" was gemma4:12b
  "r1"     { $choice = 3 }
  "potato" { $choice = 4 }
  "medium" { $choice = 5 }
  "big"    { $choice = 6 }
  default {
    Write-Host "Which AI model should pick your highlights? (runs locally via Ollama)"
    Write-Host "  Small (pick one of these on a normal PC):"
    Write-Host "  1) qwen3.5:9b       best for low-end devices        needs  8+ GB RAM, ~6 GB download"
    Write-Host "  2) gemma4:12b       best overall for this           needs 16+ GB RAM, ~8 GB download"
    Write-Host "  3) deepseek-r1:8b   good with thinking, slower      needs  8+ GB RAM, ~5 GB download"
    Write-Host "  4) deepseek-r1:1.5b pick if you have a potato PC    needs  4+ GB RAM, ~1 GB download"
    Write-Host "  Bigger (only with serious RAM):"
    Write-Host "  5) medium -- qwen3.6:27b      needs 32+ GB RAM, ~18 GB download"
    Write-Host "  6) big    -- deepseek-r1:70b  needs 64+ GB RAM, ~43 GB download"
    Write-Host "  (DeepSeek-R1 models think before answering -- the app enables that automatically.)"
    $c = Read-Host "Choose [1-6] (your $ramGB GB RAM suggests $suggest)"
    $choice = $suggest
    if ($c -match "^[1-6]$") { $choice = [int]$c }
  }
}
$model  = @{1="qwen3.5:9b"; 2="gemma4:12b"; 3="deepseek-r1:8b"; 4="deepseek-r1:1.5b"; 5="qwen3.6:27b"; 6="deepseek-r1:70b"}[$choice]
$needGB = @{1=8; 2=16; 3=8; 4=4; 5=32; 6=64}[$choice]
if ($ramGB -lt $needGB) {
  Warn "$model wants $needGB+ GB RAM but you have $ramGB GB -- it may be painfully slow or fail."
  $r = Read-Host "Continue with $model anyway? [y/N]"
  if ($r -notmatch "^[yY]") { Die "Re-run and pick a smaller model." }
}
Ok "Model: $model"
Write-Host ""

# ---------- 4. system packages (winget) ----------
Write-Host "==> System packages" -ForegroundColor Cyan
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Die "winget not found. Update 'App Installer' from the Microsoft Store, then re-run."
}
$wingetArgs = @("--accept-source-agreements", "--accept-package-agreements", "-e", "--silent")
if (-not (Get-Command git -ErrorAction SilentlyContinue))    { Run "Install Git"    { winget install Git.Git @wingetArgs } ; Refresh-Path }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Run "Install ffmpeg" { winget install Gyan.FFmpeg @wingetArgs } ; Refresh-Path }
if (-not (Get-Command node -ErrorAction SilentlyContinue))   { Run "Install Node.js (yt-dlp needs it for YouTube)" { winget install OpenJS.NodeJS.LTS @wingetArgs } ; Refresh-Path }

# ---------- 5. Python 3.9-3.12 (mediapipe's supported range) ----------
$py = $null
foreach ($cand in @("py -3.12", "py -3.11", "py -3.10", "python")) {
  try {
    $v = Invoke-Expression "$cand -c `"import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')`"" 2>$null
    if ($v -in @("3.9","3.10","3.11","3.12")) { $py = $cand; break }
  } catch {}
}
if (-not $py) {
  Run "Install Python 3.12" { winget install Python.Python.3.12 @wingetArgs }
  Refresh-Path
  $py = "py -3.12"
}
Ok "Python: $(Invoke-Expression "$py --version")"

# ---------- 6. Ollama ----------
Write-Host "==> Ollama (local LLM runtime)" -ForegroundColor Cyan
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Run "Install Ollama" { winget install Ollama.Ollama @wingetArgs }
  Refresh-Path
} else { Ok "Ollama already installed" }
try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 2 | Out-Null }
catch { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }

# ---------- 7. the app ----------
Write-Host "==> 2d.shorts code" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Base, $Bin | Out-Null
if (Test-Path (Join-Path $App ".git")) {
  Run "Update existing install" { git -C $App pull --ff-only }
} else {
  Run "Clone the repo" { git clone --depth 1 $RepoUrl $App }
}
if (-not (Test-Path $Venv)) { Run "Create Python environment" { Invoke-Expression "$py -m venv `"$Venv`"" } }
# Use python.exe -m pip, not pip.exe directly -- on Windows pip.exe can't
# overwrite itself while it's the running process, which makes a self-upgrade
# via pip.exe fail with "To modify pip, please run the following command...".
$pyExe = Join-Path $Venv "Scripts\python.exe"
Run "Upgrade pip" { & $pyExe -m pip install --quiet --upgrade pip }
Run "Install Python dependencies (this is the big one, be patient)" { & $pyExe -m pip install --quiet -r (Join-Path $App "requirements.txt") }

# ---------- 8. pull the model ----------
if ($env:SHORTS_SKIP_MODELS -ne "1") {
  Write-Host "==> AI model download ($model) -- grab a coffee, this is gigabytes" -ForegroundColor Cyan
  Run "ollama pull $model" { ollama pull $model }
}

# ---------- 9. config + the `shorts` command ----------
Write-Host "==> Finishing up" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Data | Out-Null
@{ llm_model = $model; llm_base_url = "http://localhost:11434" } |
  ConvertTo-Json | Set-Content (Join-Path $Data "config.json") -Encoding UTF8
Ok "Config written ($Data\config.json)"

Copy-Item (Join-Path $App "scripts\shorts.cmd") (Join-Path $Bin "shorts.cmd") -Force
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Bin*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$Bin", "User")
  Warn "Added $Bin to your PATH -- open a NEW terminal before using 'shorts'."
}
Ok "Installed the 'shorts' command"

# ---------- 10. the guide ----------
Write-Host ""
Write-Host "   ==========================================" -ForegroundColor Green
Write-Host "   =                                        =" -ForegroundColor Green
Write-Host "   =   INSTALLED. YOU'RE READY TO COOK.     =" -ForegroundColor Green
Write-Host "   =                                        =" -ForegroundColor Green
Write-Host "   ==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "How to use it -- one command (in a NEW terminal):"
Write-Host ""
Write-Host "    shorts" -ForegroundColor Cyan
Write-Host ""
Write-Host "  It will ask whether you want the Web UI (point-and-click in your"
Write-Host "  browser) or the CLI (flags & scripting). That's it."
Write-Host ""
Write-Host "  Quick taste of the CLI:" -ForegroundColor DarkGray
Write-Host '    shorts "https://youtube.com/watch?v=..." --num-clips 3'
Write-Host '    shorts video.mp4 -m "the part where they argue about the budget"'
Write-Host ""
Write-Host "  YouTube blocking downloads? Export browser cookies to:" -ForegroundColor DarkGray
Write-Host "    $Data\cookies.txt"
Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  Made with <3 by 2d.jack -- https://github.com/2d-jack"
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
