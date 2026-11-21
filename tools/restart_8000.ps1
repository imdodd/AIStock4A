# Restart the AIStock4A backend on port 8000 and verify the new endpoints.
#
# Two modes, auto-detected:
#   1) SERVICE mode (default since the 2026-11-20 cutover): 8000 is owned by the
#      Windows service AStockTracker (nssm). We must restart it through nssm /
#      the service control manager. Do NOT kill the child python directly --
#      nssm would either respawn it behind our back or leave the service stuck
#      in Paused, and the new process would not be a proper service child.
#   2) STANDALONE mode (legacy): uvicorn was started by hand, so we stop the PID
#      holding the port and start a fresh uvicorn ourselves.
#
# Usage (must be an ADMIN PowerShell):
#   powershell -ExecutionPolicy Bypass -File D:\code\AIStock4A\tools\restart_8000.ps1
#   ... -DryRun     just report what would happen
#   ... -Yes        skip the interactive confirmation (unattended runs)
#   ... -Mode Nssm  force a mode: Nssm | Standalone | Auto (default)

param(
    [int]$Port = 8000,
    [switch]$DryRun,
    [switch]$Yes,                 # skip the manual confirmation when cmdline is unreadable
    [ValidateSet("Auto", "Nssm", "Standalone")]
    [string]$Mode = "Auto",
    [string]$NssmPath = "",       # path to nssm.exe; service mode needs it
    [string]$Svc = ""             # service name; default AStockTracker
)

$ErrorActionPreference = "Stop"

# repo root = parent of the tools/ directory this script lives in
$Root    = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Backend = Join-Path $Root "backend"
$Python  = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir  = Join-Path $Root "logs"
$SvcName = if ($Svc) { $Svc } else { "AStockTracker" }

function Write-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "   [ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "   [!!] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "   [xx] $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- preflight
Write-Step "Preflight"

# locate nssm.exe: -NssmPath param > PATH > common install locations
if ($NssmPath -and (Test-Path $NssmPath)) {
    $Nssm = $NssmPath
} else {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Nssm = $cmd.Source }
    else {
        $cands = @("C:\nssm\nssm.exe", "C:\tools\nssm\nssm.exe") | Where-Object { Test-Path $_ }
        if ($cands) { $Nssm = $cands[0] }
        else { $Nssm = "" }
    }
}
if (-not $Nssm) { Write-Warn2 "nssm.exe not found - SERVICE mode unavailable (use -NssmPath or install nssm into PATH)" }
else { Write-Ok "nssm: $Nssm" }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Write-Ok "running as administrator" }
else { Write-Warn2 "NOT administrator - stopping the old process will probably fail" }

if (-not (Test-Path $Python)) { Write-Err2 "python not found: $Python"; exit 1 }
Write-Ok "python: $Python"

if (-not (Test-Path (Join-Path $Backend "main.py"))) {
    Write-Err2 "main.py not found under $Backend"; exit 1
}
Write-Ok "backend: $Backend"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# ---------------------------------------------------------------- pick mode
Write-Step "Detecting how port $Port is managed"

$svc = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
$useNssm = $false
if ($Mode -eq "Nssm") { $useNssm = $true }
elseif ($Mode -eq "Standalone") { $useNssm = $false }
else { if ($svc) { $useNssm = $true } }

if ($useNssm) {
    if (-not $svc) { Write-Err2 "service $SvcName not found; cannot use nssm mode"; exit 2 }
    if (-not $Nssm -or -not (Test-Path $Nssm)) { Write-Err2 "nssm.exe not found; use -NssmPath <path>"; exit 2 }
    Write-Ok "$SvcName is a service (status=$($svc.Status)) -> nssm mode"
    foreach ($k in @("Application", "AppParameters", "AppDirectory")) {
        $v = (& $Nssm get $SvcName $k 2>&1) -join " "
        Write-Host "   $k = $v"
    }
} else {
    Write-Ok "no $SvcName service -> standalone mode (legacy)"
}

# ---------------------------------------------------------------- helper: wait until HTTP answers
function Wait-Api([string]$path, [int]$tries = 40, [int]$sleepMs = 700) {
    for ($i = 0; $i -lt $tries; $i++) {
        Start-Sleep -Milliseconds $sleepMs
        try {
            $r = Invoke-WebRequest ("http://127.0.0.1:$Port" + $path) -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

# ================================================================ SERVICE MODE
if ($useNssm) {

    if ($DryRun) {
        Write-Warn2 "DryRun: would run 'nssm restart $SvcName' and verify /api/health"
        exit 0
    }

    Write-Step "Restarting service $SvcName"

    if (-not $Yes -and -not $isAdmin) {
        Write-Warn2 "stopping a LocalSystem service needs an ADMIN PowerShell."
        $ans = Read-Host "   Press Enter to continue anyway, or type N to abort"
        if ($ans -match '^[nN]') { Write-Host "   aborted by user"; exit 0 }
    }

    # nssm restart = stop (wait) + start (wait), which is what we want.
    & $Nssm restart $SvcName
    if ($LASTEXITCODE -ne 0) {
        Write-Err2 "nssm restart failed (exit=$LASTEXITCODE)"
        Write-Warn2 "check: nssm status $SvcName / service-err.log"
        exit 3
    }
    Write-Ok "nssm restart returned 0"

    # nssm already waited, but confirm the SCM agrees
    $svc2 = Get-Service -Name $SvcName
    Write-Ok "service status = $($svc2.Status)"

    $ready = Wait-Api "/api/health"
    if ($ready) { Write-Ok "service is up" }
    else {
        Write-Err2 "service did not answer /api/health within ~28s"
        $errLog = Join-Path $Backend "data-live\logs\service-err.log"
        if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "   $_" } }
        exit 4
    }

# ================================================================ STANDALONE MODE
} else {

    Write-Step "Port $Port"

    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $procId = ($conn | Select-Object -First 1).OwningProcess
        $proc   = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        $name   = ""
        $cmd    = ""
        if ($proc) {
            $name = [string]$proc.Name
            $cmd  = [string]$proc.CommandLine
        }
        Write-Host "   holder: PID=$procId  name=$name"
        if ($cmd) { Write-Host "   cmd: $cmd" }
        else { Write-Warn2 "command line not readable (process runs as another user)" }

        # Only kill when it is clearly our uvicorn. Blank cmdline is accepted when
        # the name is python, because an elevated process hides its cmdline.
        $looksOurs = $false
        if ($name -match '^python') {
            if ($cmd -match 'uvicorn') { $looksOurs = $true }
            elseif ([string]::IsNullOrWhiteSpace($cmd)) { $looksOurs = $true }
        }
        if (-not $looksOurs) {
            Write-Err2 "the process on port $Port does not look like our uvicorn; aborting"
            exit 5
        }

        if ($DryRun) {
            Write-Warn2 "DryRun: would stop PID $procId and start a new one"
            exit 0
        }

        if ([string]::IsNullOrWhiteSpace($cmd) -and -not $Yes) {
            Write-Warn2 "cannot read its command line, so its identity is not verified."
            Write-Host "   About to stop PID $procId." -ForegroundColor Yellow
            $ans = Read-Host "   Press Enter to continue, or type N to abort"
            if ($ans -match '^[nN]') { Write-Host "   aborted by user"; exit 0 }
        }

        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Ok "stopped PID $procId"
        } catch {
            Write-Err2 "failed to stop PID $procId : $($_.Exception.Message)"
            Write-Warn2 "run this script from an ADMIN PowerShell"
            exit 6
        }

        $freed = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 400
            $still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if (-not $still) { $freed = $true; break }
        }
        if ($freed) { Write-Ok "port $Port released" }
        else { Write-Err2 "port $Port still occupied"; exit 7 }
    } else {
        Write-Ok "port $Port is free"
        if ($DryRun) { Write-Warn2 "DryRun: would start uvicorn on $Port"; exit 0 }
    }

    Write-Step "Starting uvicorn on $Port"

    $stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
    $outLog  = Join-Path $LogDir "server-$Port-$stamp.out.log"
    $errLog  = Join-Path $LogDir "server-$Port-$stamp.err.log"

    $argv = @(
        "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--log-level", "warning"
    )

    # Environment variables are inherited by the child process. .env also carries
    # ASTOCK_DATA_DIR, but passing it explicitly keeps the child independent of cwd.
    $oldDataDir = $env:ASTOCK_DATA_DIR
    $env:ASTOCK_DATA_DIR = Join-Path $Backend "data-live"
    try {
        $p = Start-Process -FilePath $Python -ArgumentList $argv -WorkingDirectory $Backend `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    } finally {
        if ($null -eq $oldDataDir) { Remove-Item Env:\ASTOCK_DATA_DIR -ErrorAction SilentlyContinue }
        else { $env:ASTOCK_DATA_DIR = $oldDataDir }
    }

    Write-Ok "started PID $($p.Id)"
    Write-Host "   stdout: $outLog"
    Write-Host "   stderr: $errLog"

    Write-Step "Waiting for the service"
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 700
        if ($p.HasExited) {
            Write-Err2 "process exited with code $($p.ExitCode)"
            if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "   $_" } }
            exit 8
        }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/holdings" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if ($ready) { Write-Ok "service is up (PID $($p.Id))" }
    else { Write-Err2 "service did not answer within ~28s"; exit 9 }
}

# ================================================================ VERIFY (both modes)
Write-Step "Verifying endpoints"

$base = "http://127.0.0.1:$Port"

function Test-Api($path) {
    try {
        $r = Invoke-WebRequest ($base + $path) -UseBasicParsing -TimeoutSec 20
        $obj = $r.Content | ConvertFrom-Json
        return @{ ok = ($r.StatusCode -eq 200); code = $r.StatusCode; data = $obj.data }
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        return @{ ok = $false; code = $code; data = $null }
    }
}

$fail = 0

# boot_at tells us whether the process really restarted (the whole point of this
# script is picking up new backend code -- a stale boot_at means we failed).
$h = Test-Api "/api/health"
if ($h.ok) { Write-Ok "/api/health  200  boot_at=$($h.data.boot_at)  code_file=$($h.data.code_file)  stale=$($h.data.stale)" }
else { Write-Err2 "/api/health  HTTP $($h.code)"; $fail++ }

$a = Test-Api "/api/holdings/realized"
if ($a.ok) {
    $d = $a.data
    $withBuy = 0
    foreach ($e in $d.entries) { if ($e.buy_date) { $withBuy++ } }
    Write-Ok "/api/holdings/realized  200  entries=$($d.count)  with_buy_date=$withBuy  total=$($d.total)"
} else { Write-Err2 "/api/holdings/realized  HTTP $($a.code)"; $fail++ }

$b = Test-Api "/api/holdings/pnl/calendar"
if ($b.ok) {
    Write-Ok "/api/holdings/pnl/calendar  200  month=$($b.data.month)  trade_days=$($b.data.trade_days)  detail_days=$($b.data.detail_days)"
} else { Write-Err2 "/api/holdings/pnl/calendar  HTTP $($b.code)"; $fail++ }

# note endpoint: write then delete, so nothing is left behind
$c = Test-Api "/api/holdings/realized"
if ($c.ok -and $c.data.entries.Count -gt 0) {
    $key = $c.data.entries[0].key
    $body = @{ key = $key; note = "__restart_probe__" } | ConvertTo-Json -Compress
    try {
        Invoke-WebRequest "$base/api/holdings/realized/note" -Method Put `
            -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 10 | Out-Null
        $body2 = @{ key = $key; note = "" } | ConvertTo-Json -Compress
        Invoke-WebRequest "$base/api/holdings/realized/note" -Method Put `
            -ContentType "application/json" -Body $body2 -UseBasicParsing -TimeoutSec 10 | Out-Null
        Write-Ok "/api/holdings/realized/note  PUT 200 (probe written and removed)"
    } catch {
        Write-Err2 "/api/holdings/realized/note  failed: $($_.Exception.Message)"
        $fail++
    }
}

# ---------------------------------------------------------------- summary
Write-Step "Result"
Write-Host ("   mode = " + $(if ($useNssm) { "nssm service" } else { "standalone" }))
if ($fail -eq 0) {
    Write-Host "   All endpoints OK. Reload the page with Ctrl+F5." -ForegroundColor Green
} else {
    Write-Host "   $fail check(s) failed. See the logs above." -ForegroundColor Red
}
Write-Host ""
exit $fail
