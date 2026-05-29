<#
.SYNOPSIS
    One-command trading-day setup: scrape signals, validate config, wait for
    CSVs, compute trade plan, start servers, open calculator with auto-import.

.DESCRIPTION
    Replaces the 5-step Section 3 "Daily Workflow" in USER_GUIDE.md with a
    single command.  Sequence:
      1. Scrape SectorSurfer signals  (prompts to reuse if file is fresh)
      2. Validate accounts.json + signals.json
      3. Wait for today's Fidelity CSVs in Downloads
      4. Run cli.compute -> state.json
      5. Start Yahoo proxy / static server (run.ps1 -NoLaunch) if not running
      6. Open calculator in Chrome with ?import=state.json
      7. Print summary

.EXAMPLE
    .\scripts\morning-prep.ps1
#>

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$FRDir       = Join-Path $ProjectRoot "fidelity_rebalancer"
$SignalsFile = Join-Path $ProjectRoot "signals.json"
$StateFile   = Join-Path $ProjectRoot "state.json"
$Downloads   = [Environment]::GetFolderPath("UserProfile") + "\Downloads"
$Port        = 7823
$ProxyPort   = 7824
$ProxyUrl    = "http://localhost:$ProxyPort/fetch_closes?tickers=SPY"

function Write-Step  { param($n, $msg) Write-Host "" ; Write-Host "  [$n] $msg" -ForegroundColor Cyan }
function Write-Pass  { param($msg)     Write-Host "      OK  $msg" -ForegroundColor Green }
function Write-Warn  { param($msg)     Write-Host "      WARN $msg" -ForegroundColor Yellow }
function Bail        { param($msg)     Write-Host "" ; Write-Host "  ERROR: $msg" -ForegroundColor Red ; exit 1 }

Write-Host ""
Write-Host "  Morning Prep - Fidelity Rebalancer" -ForegroundColor White
Write-Host "  ------------------------------------" -ForegroundColor DarkGray

# -- Step 1: SectorSurfer signals ----------------------------------------------

Write-Step 1 "SectorSurfer signals"

$rescrape = $true
if (Test-Path $SignalsFile) {
    $age = (Get-Date) - (Get-Item $SignalsFile).LastWriteTime
    $ageMin = [int]$age.TotalMinutes
    if ($age.TotalHours -lt 6) {
        Write-Host "      signals.json is $ageMin minutes old." -ForegroundColor DarkGray
        $ans = Read-Host "      Re-scrape? [y/N]"
        $rescrape = ($ans -match "^[yY]")
    }
}

if ($rescrape) {
    Write-Host "      Running scraper..." -ForegroundColor DarkGray
    & python "$ProjectRoot\scripts\sectorsurfer_signals.py" --out $SignalsFile
    if ($LASTEXITCODE -ne 0) { Bail "Scraper failed (exit $LASTEXITCODE). Check the Chromium window." }
    Write-Pass "signals.json written"
} else {
    Write-Pass "Reusing existing signals.json"
}

# -- Step 2: Validate config ---------------------------------------------------

Write-Step 2 "Validate config"
& python "$ProjectRoot\scripts\validate_config.py" `
    --accounts "$FRDir\accounts.json" `
    --signals  $SignalsFile
if ($LASTEXITCODE -ne 0) { Bail "Config validation failed. Fix accounts.json or signals.json before continuing." }
Write-Pass "accounts.json + signals.json valid"

# -- Step 3: Wait for today's Fidelity CSVs -----------------------------------

Write-Step 3 "Fidelity position CSVs"

function Get-TodayCSVs {
    $today = (Get-Date).Date
    return Get-ChildItem $Downloads -Filter "*.csv" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime.Date -eq $today }
}

$csvs = Get-TodayCSVs
if (-not $csvs) {
    Write-Host ""
    Write-Host "      No Fidelity CSVs dated today found in:" -ForegroundColor Yellow
    Write-Host "      $Downloads" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      Download all 3 account CSVs from Fidelity.com, then press Enter." -ForegroundColor DarkGray
    Write-Host "      (Ctrl+C to abort)" -ForegroundColor DarkGray
    Read-Host "      Press Enter when ready to poll"

    $deadline = (Get-Date).AddMinutes(10)
    Write-Host "      Polling Downloads every 5s (10 min timeout)..." -ForegroundColor DarkGray
    while (-not (Get-TodayCSVs)) {
        if ((Get-Date) -gt $deadline) { Bail "Timed out waiting for CSVs after 10 minutes." }
        Start-Sleep -Seconds 5
        Write-Host "      ..." -ForegroundColor DarkGray -NoNewline
    }
    $csvs = Get-TodayCSVs
    Write-Host ""
}

$csvNames = ($csvs | ForEach-Object { $_.Name }) -join ", "
Write-Pass "$($csvs.Count) CSV(s) found: $csvNames"

# -- Step 4: Compute trade plan ------------------------------------------------

Write-Step 4 "Compute trade plan (cli.compute)"

if (Test-Path $StateFile) {
    $stateAge = (Get-Date) - (Get-Item $StateFile).LastWriteTime
    if ($stateAge.TotalHours -lt 2) {
        Write-Warn "state.json already exists and is $([int]$stateAge.TotalMinutes) minutes old - overwriting."
    }
}

$prevPYTHONPATH = $env:PYTHONPATH
$env:PYTHONPATH = $FRDir
Push-Location $FRDir
try {
    & python -m cli.compute --signals $SignalsFile --export $StateFile --inputs $Downloads
    if ($LASTEXITCODE -ne 0) { Bail "cli.compute failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
    $env:PYTHONPATH = $prevPYTHONPATH
}
Write-Pass "state.json written: $StateFile"

# -- Step 5: Start Yahoo proxy / static server if not running -----------------

Write-Step 5 "Yahoo Finance proxy + static server"


# Evict any stale Python processes holding our ports from previous runs.
function Clear-Port {
    param([int]$p)
    $procIds = netstat -ano 2>$null |
        Select-String ":$p\s" |
        Where-Object { $_ -match 'LISTENING' } |
        ForEach-Object { ($_ -split '\s+')[-1] } |
        Where-Object { $_ -match '^\d+$' } |
        Select-Object -Unique
    foreach ($id in $procIds) {
        $proc = Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like 'python*') {
            Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue
            Write-Host "      Cleared stale python PID $id on port $p" -ForegroundColor DarkYellow
        }
    }
}

# TCP-level port check — avoids hitting /fetch_closes (which calls Yahoo and
# can take 1-3s, longer than any reasonable HTTP timeout).
function Test-Port {
    param([int]$p)
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $tcp.ConnectAsync("localhost", $p)
        if ($task.Wait(500)) { return $tcp.Connected }
        return $false
    } catch { return $false } finally { $tcp.Close() }
}

$proxyRunning = Test-Port $ProxyPort

if ($proxyRunning) {
    Write-Pass "Proxy already running on port $ProxyPort"
} else {
    Clear-Port $ProxyPort
    Clear-Port $Port
    Write-Host "      Opening server window (cd <root>; .\run.ps1)..." -ForegroundColor DarkGray
    # Mirror the manual workflow exactly: cd to project root, then .\run.ps1
    # with no flags. run.ps1 handles checks, proxy, static server, and Chrome.
    # -NoExit keeps the window open if anything fails so we can read the error.
    $cmd = "Set-Location '$ProjectRoot'; .\run.ps1"
    Start-Process powershell `
        -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd `
        -WindowStyle Normal

    # Poll TCP port until proxy is listening (60s timeout — run.ps1 does ~5s of checks first)
    $deadline = (Get-Date).AddSeconds(60)
    while (-not $proxyRunning -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $proxyRunning = Test-Port $ProxyPort
    }
    if (-not $proxyRunning) { Bail "Proxy did not start within 60 seconds. Check the server window." }
    Write-Pass "Proxy + static server running (port $ProxyPort / $Port)"
}

# -- Step 6: Summary -----------------------------------------------------------

Write-Host ""
Write-Host "  ============================================" -ForegroundColor DarkGray
Write-Host "  Morning prep complete" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor DarkGray

$signalCount = 0
try {
    $sig = Get-Content $SignalsFile | ConvertFrom-Json
    $signalCount = ($sig.signals.PSObject.Properties | Measure-Object).Count
} catch { }

Write-Host "  Signals  : $signalCount strategies" -ForegroundColor White
Write-Host "  CSVs     : $($csvs.Count) file(s) - $csvNames" -ForegroundColor White
Write-Host "  State    : $StateFile" -ForegroundColor White
Write-Host "  URL      : http://localhost:$Port/rebalance_calculator.html" -ForegroundColor White
Write-Host ""
Write-Host "  Chrome should already be open (launched by run.ps1)." -ForegroundColor DarkGray
Write-Host "  In the calculator, click Import State and select:" -ForegroundColor DarkGray
Write-Host "    $StateFile" -ForegroundColor DarkGray
Write-Host ""
