# start_streamlit.ps1
# Daemon wrapper for the quant backtest Streamlit app.
# - Starts Streamlit on 127.0.0.1:8501, logs in .\logs\
# - If 8501 is already serving, exits immediately (no duplicate instance).
# - Restarts Streamlit automatically after an abnormal exit, with growing backoff.
# - Exits when Streamlit stops cleanly (exit code 0 after a healthy run) => manual stop.

$ErrorActionPreference = 'Continue'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir     = Join-Path $ProjectDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$EventsLog = Join-Path $LogDir 'streamlit_events.log'
$RunLog    = Join-Path $LogDir 'streamlit_run.log'
$Python    = 'D:\Anaconda3\python.exe'

function Write-Event([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $EventsLog -Value $line -Encoding utf8 } catch {}
}

function Test-PortUp {
    $r = Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $r)
}

if (Test-PortUp) {
    Write-Event '8501 already listening - skip (no duplicate).'
    exit 0
}

$backoff = 5
$restartCount = 0

while ($true) {
    if (Test-PortUp) {
        Write-Event '8501 became listening meanwhile - exit.'
        exit 0
    }

    $startTime = Get-Date
    Write-Event "Starting Streamlit (restartCount=$restartCount)"
    # Run through cmd.exe so stdout+stderr both append into one log file.
    $cmdline = '"' + $Python + '" -X utf8 -m streamlit run app.py --server.headless true --server.port 8501 --browser.gatherUsageStats false >> "' + $RunLog + '" 2>&1'
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cmdline `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
    try { Wait-Process -Id $p.Id -ErrorAction Stop } catch {}
    try { $p.Refresh(); $exitCode = $p.ExitCode } catch { $exitCode = -1 }

    $ranSeconds = ((Get-Date) - $startTime).TotalSeconds
    Write-Event ("Streamlit exited code={0} ran={1:N0}s" -f $exitCode, $ranSeconds)

    if ($exitCode -eq 0 -and $ranSeconds -gt 15) {
        Write-Event 'Clean exit (code 0) - treat as manual stop, daemon quits.'
        exit 0
    }

    $restartCount++
    if ($ranSeconds -ge 30) { $backoff = 5 }
    Write-Event "Abnormal exit - restart in ${backoff}s"
    Start-Sleep -Seconds $backoff
    if ($backoff -lt 60) { $backoff += 10 }
}
