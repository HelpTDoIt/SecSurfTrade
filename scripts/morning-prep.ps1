<#
.SYNOPSIS
    One-command trading-day setup: scrape signals, validate config, wait for
    CSVs, compute trade plan, start servers, open calculator with auto-import.

.DESCRIPTION
    Replaces the 5-step Section 3 "Daily Workflow" in USER_GUIDE.md (repo root) with a
    single command.  Sequence:
      1. Scrape SectorSurfer signals  (prompts to reuse if file is fresh)
      2. Validate accounts.json + signals.json
      3. Wait for today's Fidelity CSVs in Downloads
      4. Run cli.compute -> state.json
      5. Order-sizing preflight (opt-in): FT+ readiness gate -> cli.strategy
         sizing -> sanity gate -> next steps (cli.preflight)
      6. Start Yahoo proxy / static server (run.ps1 -NoLaunch) if not running
      7. Open calculator in Chrome with ?import=state.json
      8. Print summary

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
function Write-Ref   { param($msg)     Write-Host "      USER_GUIDE: $msg" -ForegroundColor DarkGray }
function Write-Pass  { param($msg)     Write-Host "      OK  $msg" -ForegroundColor Green }
function Write-Warn  { param($msg)     Write-Host "      WARN $msg" -ForegroundColor Yellow }
function Bail        { param($msg)     Write-Host "" ; Write-Host "  ERROR: $msg" -ForegroundColor Red ; exit 1 }

Write-Host ""
Write-Host "  Morning Prep - Fidelity Rebalancer" -ForegroundColor White
Write-Host "  ------------------------------------" -ForegroundColor DarkGray

# -- Step 1: SectorSurfer signals ----------------------------------------------

Write-Step 1 "SectorSurfer signals"
Write-Ref "Sec 4, Step 1 - Get SectorSurfer signals (scripts/sectorsurfer_signals.py)"

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
Write-Ref "Sec 6 - scripts/validate_config.py (accounts.json + signals.json)"
& python "$ProjectRoot\scripts\validate_config.py" `
    --accounts "$FRDir\accounts.json" `
    --signals  $SignalsFile
if ($LASTEXITCODE -ne 0) { Bail "Config validation failed. Fix accounts.json or signals.json before continuing." }
Write-Pass "accounts.json + signals.json valid"

# -- Step 3: Wait for today's Fidelity CSVs -----------------------------------

Write-Step 3 "Fidelity position CSVs"
Write-Ref "Sec 4, Step 2 - Download Fidelity CSVs (3 accounts -> Downloads)"

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

# Duplicate-download guard: if morning-prep runs more than once a day, today's
# Downloads can contain multiple CSVs for the same account (Fidelity uses the
# same date-based filename for every account, so the browser disambiguates with
# " (1)", " (2)", ... — those suffixes do NOT identify the account). Group by
# the Account Name inside each CSV and keep the newest per account.
function Get-CsvAccountName {
    param([string]$Path)
    # Mirrors fidelity_rebalancer/engine/calculator.py parse_csv + consolidate:
    # skip metadata rows that start with '"', treat the first remaining line as
    # the header (Account Number, Account Name, Symbol, ...), find the
    # 'Account Name' column index, then read it from the first data row.
    # Column 0 is the Account NUMBER, not the name — earlier versions grouped
    # every CSV under the literal string 'Account Number' because of that.
    try {
        $lines = Get-Content -Path $Path -TotalCount 100 -ErrorAction Stop |
            Where-Object { $_.Trim() -and -not $_.StartsWith('"') }
        if (-not $lines -or $lines.Count -lt 2) { return $null }
        $hdr = $lines[0] -split ','
        $idx = -1
        for ($i = 0; $i -lt $hdr.Count; $i++) {
            if ($hdr[$i].Trim() -eq 'Account Name') { $idx = $i; break }
        }
        if ($idx -lt 0) { return $null }
        $parts = $lines[1] -split ','
        if ($parts.Count -le $idx) { return $null }
        $name = $parts[$idx].Trim().Trim('"')
        if ($name) { return $name }
    } catch { }
    return $null
}

$csvsByAccount = @{}
foreach ($f in $csvs) {
    $acct = Get-CsvAccountName -Path $f.FullName
    if (-not $acct) { continue }
    if (-not $csvsByAccount.ContainsKey($acct)) { $csvsByAccount[$acct] = @() }
    $csvsByAccount[$acct] += $f
}
$dupAccounts = $csvsByAccount.GetEnumerator() | Where-Object { $_.Value.Count -gt 1 }
if ($dupAccounts) {
    Write-Host ""
    Write-Warn "Multiple CSVs for the same account in Downloads (re-run mid-day, or extra clicks):"
    foreach ($entry in $dupAccounts) {
        $dupNames = ($entry.Value | Sort-Object LastWriteTime -Descending | ForEach-Object { $_.Name }) -join ", "
        Write-Host "        $($entry.Key): $dupNames" -ForegroundColor Yellow
    }
    Write-Host "      cli.compute already picks the newest by mtime, but stale copies clutter Downloads." -ForegroundColor DarkGray
    $ans = Read-Host "      Delete the older copies now, keeping the newest per account? [y/N]"
    if ($ans -match "^[yY]") {
        foreach ($entry in $dupAccounts) {
            $entry.Value | Sort-Object LastWriteTime -Descending | Select-Object -Skip 1 |
                ForEach-Object {
                    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                    Write-Host "        Removed $($_.Name) ($($entry.Key))" -ForegroundColor DarkYellow
                }
        }
        $csvs = Get-TodayCSVs
        $csvNames = ($csvs | ForEach-Object { $_.Name }) -join ", "
        Write-Pass "$($csvs.Count) CSV(s) after dedupe: $csvNames"
    } else {
        Write-Warn "Keeping duplicates. cli.compute will use the newest per account by mtime."
    }
}

# -- Step 4: Compute trade plan ------------------------------------------------

Write-Step 4 "Compute trade plan (cli.compute)"
Write-Ref "Sec 4, Step 3 - Compute the trade plan (cli.compute -> state.json)"

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

# -- Step 5: Order-sizing preflight (opt-in) ----------------------------------

Write-Step 5 "Order-sizing preflight (FT+ readiness -> sizing -> sanity gate)"
Write-Ref "Sec 6 - cli.preflight --state state.json (readiness, sizing, sanity gate)"
Write-Host "      Requires Fidelity Trader+ open with the Watchlist + L2 windows." -ForegroundColor DarkGray
$runPreflight = Read-Host "      Run order-sizing preflight now? [Y/n]"
if ($runPreflight -notmatch "^[nN]") {
    $prevPYTHONPATH = $env:PYTHONPATH
    $env:PYTHONPATH = $FRDir
    Push-Location $FRDir
    try {
        # --no-next-steps: morning-prep prints ONE consolidated next-steps block
        # at the very end (Step 7), so preflight must not print its own mid-run.
        & python -m cli.preflight --state $StateFile --no-next-steps
        $preflightExit = $LASTEXITCODE
    } finally {
        Pop-Location
        $env:PYTHONPATH = $prevPYTHONPATH
    }
    if ($preflightExit -eq 0) {
        Write-Pass "Preflight complete - state sized and sanity-checked."
    } else {
        # Non-zero = user abort or RED sanity gate. Don't trade on it, but let
        # morning-prep finish so the calculator path stays available.
        Write-Warn "Preflight exited $preflightExit (aborted or RED gate). Review before entering orders."
    }
} else {
    Write-Warn "Skipped order-sizing preflight. Size orders before trading."
}

# -- Step 6: Start Yahoo proxy / static server if not running -----------------

Write-Step 6 "Yahoo Finance proxy + static server"
Write-Ref "Sec 2 - Launch the app (run.ps1: proxy on 7824, calculator on 7823)"


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

# TCP-level port check - avoids hitting /fetch_closes (which calls Yahoo and
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

# Launch the app EXACTLY like the manual workflow: open a 2nd PowerShell window
# and run .\run.ps1 (no flags). run.ps1 starts the proxy, the static server, AND
# opens Chrome on the calculator - all in one place. Doing this unconditionally
# (instead of trying to detect an already-running server and skip) is what keeps
# the calculator opening reliably: the old skip-if-port-busy check only probed
# the proxy port 7824, so a half-running state (7824 held, 7823 dead) made the
# script think all was well and open Chrome to a server that wasn't there.
#
# Clear any stale python servers on our ports first so run.ps1 can bind cleanly.
Clear-Port $ProxyPort
Clear-Port $Port
Write-Host "      Opening server window (cd <root>; .\run.ps1)..." -ForegroundColor DarkGray
# -NoExit keeps the window open if anything fails so we can read the error.
$cmd = "Set-Location '$ProjectRoot'; .\run.ps1"
Start-Process powershell `
    -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd `
    -WindowStyle Normal

# Poll TCP port until proxy is listening (60s timeout - run.ps1 does ~5s of checks first)
$proxyRunning = $false
$deadline = (Get-Date).AddSeconds(60)
while (-not $proxyRunning -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $proxyRunning = Test-Port $ProxyPort
}
if (-not $proxyRunning) { Bail "Proxy did not start within 60 seconds. Check the server window." }
Write-Pass "Proxy + static server running (port $ProxyPort / $Port). Chrome opened by run.ps1."

# -- Step 7: Summary -----------------------------------------------------------

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
Write-Host ""
Write-Host "  Next steps (manual order entry - the app never places orders):" -ForegroundColor White
Write-Host "    1. In the calculator (Chrome should be open), click Import State and select:" -ForegroundColor DarkGray
Write-Host "         $StateFile" -ForegroundColor DarkGray
Write-Host "    2. Enter each order manually in Fidelity Trader+, the sized chunks in order:" -ForegroundColor DarkGray
Write-Host "       sells first, then buys, following the limit prices in the sized state." -ForegroundColor DarkGray
Write-Host "    3. After fills, run the EOD report to capture the session:" -ForegroundColor DarkGray
Write-Host "         Set-Location '$FRDir'; `$env:PYTHONPATH = '.'; python -m cli.eod_report" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Full daily workflow: see Section 3 of USER_GUIDE.md" -ForegroundColor DarkGray
Write-Host ""
