<#
.SYNOPSIS
    Bootstrap and launch the Fidelity Rebalancer.

.DESCRIPTION
    Verifies Python 3.12+, installs required packages and the Playwright
    Chromium browser if missing, then starts the React calculator on
    http://localhost:7823/rebalance_calculator.html

    Also starts a lightweight Yahoo Finance proxy server on port 7824
    so the browser can fetch previous-day closes without CORS issues.

    All dependency installs happen automatically.  Any step that cannot
    be fixed automatically prints a plain error and exits cleanly.

.EXAMPLE
    .\run.ps1

.EXAMPLE
    .\run.ps1 -SkipChecks
#>

param([switch]$SkipChecks)

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
$FRDir       = Join-Path $ProjectRoot "fidelity_rebalancer"
$Port        = 7823
$ProxyPort   = 7824

function Write-Step { param($msg) Write-Host $msg -NoNewline -ForegroundColor Cyan }
function Write-Pass { param($msg) Write-Host " $msg" -ForegroundColor Green }
function Write-Auto { param($msg) Write-Host " (installing $msg ...)" -ForegroundColor Yellow }
function Fail       { param($msg) Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not $SkipChecks) {
    Write-Host ""
    Write-Host "  Fidelity Rebalancer - startup check" -ForegroundColor White
    Write-Host "  ------------------------------------" -ForegroundColor DarkGray
    Write-Host ""

    # 1. Python 3.12+
    Write-Step "  Python 3.12+ ..........."
    try {
        $pyOut = & python --version 2>&1
        if ($LASTEXITCODE -ne 0) { Fail "Python not found in PATH.  https://www.python.org/downloads/" }
        if ($pyOut -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 12)) {
                Fail "$pyOut found but 3.12+ required.  https://www.python.org/downloads/"
            }
            Write-Pass $pyOut
        } else {
            Fail "Could not parse Python version from: $pyOut"
        }
    } catch {
        Fail "Python not found in PATH.  https://www.python.org/downloads/"
    }

    # 2. Core packages
    Write-Step "  Core packages .........."
    $coreOk = & python -c "import pydantic, pandas, yfinance, rich, loguru; print('ok')" 2>&1
    if ($coreOk -ne "ok") {
        Write-Auto "fidelity-rebalancer"
        & python -m pip install -e $FRDir --quiet 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "pip install failed.  Run manually: pip install -e `"$FRDir`"" }
        $coreOk2 = & python -c "import pydantic, pandas, yfinance, rich, loguru; print('ok')" 2>&1
        if ($coreOk2 -ne "ok") { Fail "Packages still missing after install.  Check pip output." }
        Write-Pass "installed OK"
    } else {
        Write-Pass "OK"
    }

    # 3. playwright package
    Write-Step "  playwright ............."
    $pwOk = & python -c "import playwright; print('ok')" 2>&1
    if ($pwOk -ne "ok") {
        Write-Auto "playwright"
        & python -m pip install "playwright>=1.44" --quiet 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "playwright install failed.  Run: pip install playwright" }
        Write-Pass "installed OK"
    } else {
        Write-Pass "OK"
    }

    # 4. Chromium browser — write check to temp file to avoid here-string indent rules
    Write-Step "  Chromium browser ......."
    $tmp = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content $tmp -Encoding UTF8 -Value @'
from playwright.sync_api import sync_playwright
from pathlib import Path
try:
    with sync_playwright() as pw:
        exe = pw.chromium.executable_path
        print("ok" if Path(exe).exists() else "missing")
except Exception:
    print("missing")
'@
    $chromiumOk = & python $tmp 2>&1
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if ($chromiumOk -ne "ok") {
        Write-Auto "Chromium (one-time download)"
        & python -m playwright install chromium 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "Chromium install failed.  Run: python -m playwright install chromium" }
        Write-Pass "installed OK"
    } else {
        Write-Pass "OK"
    }

    # 5. server.py
    Write-Step "  server.py .............."
    $serverScript = Join-Path $ProjectRoot "server.py"
    if (-not (Test-Path $serverScript)) {
        Fail "server.py not found in $ProjectRoot.  Re-clone or restore the file."
    }
    & python -m py_compile $serverScript 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "server.py has a syntax error.  Run: python -m py_compile server.py" }
    Write-Pass "OK"

    Write-Host ""
    Write-Host "  All checks passed." -ForegroundColor Green
    Write-Host ""
}

# Start Yahoo Finance proxy on dedicated port (hidden window, killed on exit)
$serverScript = Join-Path $ProjectRoot "server.py"
$proxy = Start-Process python -ArgumentList $serverScript, $ProxyPort `
    -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden

# Start static file server (foreground - Ctrl+C stops everything)
$url = "http://localhost:$Port/rebalance_calculator.html"
Write-Host "  Calculator:  $url" -ForegroundColor Cyan
Write-Host "  Proxy:       http://localhost:$ProxyPort/fetch_closes" -ForegroundColor DarkGray
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

# Open calculator in Chrome with remote-debugging-port=9222 so the SectorSurfer
# scraper can later open a new tab in the same window (real profile + LastPass).
$pf86 = [Environment]::GetEnvironmentVariable("PROGRAMFILES(X86)")
$chromeCandidates = @(
    "$env:PROGRAMFILES\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$pf86\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
    Start-Process $chrome -ArgumentList "--remote-debugging-port=9222", "--no-first-run", $url
    Write-Host "  Chrome:      debug port 9222 enabled (scraper will use this window)" -ForegroundColor DarkGray
} else {
    try { Start-Process $url } catch { }
    Write-Host "  Chrome not found - scraper will open its own browser." -ForegroundColor DarkYellow
}

Set-Location $ProjectRoot
try {
    & python -m http.server $Port
} finally {
    # Kill proxy when static server exits
    if ($proxy -and -not $proxy.HasExited) {
        $proxy.Kill()
        Write-Host "  Proxy stopped." -ForegroundColor DarkGray
    }
}
