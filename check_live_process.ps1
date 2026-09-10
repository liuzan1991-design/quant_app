# 明早起床后双击运行，自动检查 live_run.py 进程是否还活着
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = 'D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\live_outputs\logs'
$now = Get-Date

Write-Host '==================== Check ====================' -ForegroundColor Cyan
Write-Host ("Time: {0}" -f $now.ToString('yyyy-MM-dd HH:mm:ss dddd'))
Write-Host ''

# 1. 进程（硬判据）
$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'live_run.py' }
if ($proc) {
    Write-Host ("[OK] live_run.py running. PID={0} start={1}" -f $proc.ProcessId, $proc.CreationDate.ToString('HH:mm:ss')) -ForegroundColor Green
} else {
    Write-Host '[DEAD] live_run.py process NOT found' -ForegroundColor Red
}
Write-Host ''

# 2. 计划任务
$task = Get-ScheduledTask -TaskName 'QuantLiveRun'
if ($task) {
    $info = $task | Get-ScheduledTaskInfo
    Write-Host ("Task State : {0}" -f $task.State) -ForegroundColor Yellow
    Write-Host ("LastRun    : {0}" -f $info.LastRunTime) -ForegroundColor Yellow
    Write-Host ("LastResult : {0}" -f $info.LastTaskResult) -ForegroundColor Yellow
    Write-Host ("NextRun    : {0}" -f $info.NextRunTime) -ForegroundColor Yellow
}
Write-Host ''

# 3. 日志内容时间戳（解析内容，不用文件元数据）
$todayLog = Get-ChildItem $logDir -Filter 'live_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$lastTs = $null
if ($todayLog) {
    $lastLine = Get-Content $todayLog.FullName -Tail 1
    if ($lastLine -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        $lastTs = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
    }
    Write-Host ("Log File   : {0}" -f $todayLog.Name) -ForegroundColor Yellow
    if ($lastTs) {
        $gap = ($now - $lastTs)
        Write-Host ("Log content: {0}" -f $lastTs.ToString('HH:mm:ss')) -ForegroundColor Yellow
        Write-Host ("Gap        : {0} min {1} sec" -f [int]$gap.TotalMinutes, $gap.Seconds) -ForegroundColor Yellow
    } else {
        Write-Host 'Cannot parse timestamp from last line' -ForegroundColor Gray
    }
} else {
    Write-Host '[DEAD] no live_*.log found' -ForegroundColor Red
}
Write-Host ''

# 4. 结论：新鲜度优先。07:30-07:50 只作 stale 时的诊断提示，绝不单独触发 BAD
#    （否则修复成功后、你恰好在 07:40 查看时会误报 BAD）
Write-Host '==================== Verdict ====================' -ForegroundColor Cyan
$inDeathWindow = $false
$gapMin = 9999
if ($lastTs) {
    $t = $lastTs.TimeOfDay
    if ($t -ge [timespan]::FromHours(7.5) -and $t -le [timespan]::FromHours(7.833)) { $inDeathWindow = $true }
    $gapMin = ($now - $lastTs).TotalMinutes
}

if (-not $proc) {
    Write-Host 'BAD: process dead. Note the time, then check LastResult (1067?).' -ForegroundColor Red
} elseif (-not $lastTs) {
    Write-Host 'WARN: process alive but log timestamp unreadable.' -ForegroundColor Yellow
} elseif ($gapMin -gt 15) {
    if ($inDeathWindow) {
        Write-Host ('BAD: log stopped in 07:30-07:50 death window and is stale {0} min. Suspect second trigger.' -f [math]::Round($gapMin,0)) -ForegroundColor Red
    } else {
        Write-Host ('BAD: log stale {0} min. Process may be zombie.' -f [math]::Round($gapMin,0)) -ForegroundColor Red
    }
} elseif ($gapMin -le 5) {
    Write-Host 'GOOD: process alive, log fresh. Fix works.' -ForegroundColor Green
} else {
    Write-Host 'WARN: log gap 5-15 min. Possibly off-hours, re-check at open.' -ForegroundColor Yellow
}
Write-Host ''