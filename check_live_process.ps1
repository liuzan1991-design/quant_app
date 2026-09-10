# 明早起床后双击运行，自动检查 live_run.py 进程是否还活着
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$logDir = 'D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\live_outputs\logs'
$now = Get-Date

Write-Host '==================== Check ====================' -ForegroundColor Cyan
Write-Host ("Time: {0}" -f $now.ToString('yyyy-MM-dd HH:mm:ss dddd'))
Write-Host ''

# 1. 进程
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

# 3. 日志：解析内容里的时间戳，而不是文件元数据时间
$todayLog = Get-ChildItem $logDir -Filter 'live_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$lastTs = $null
if ($todayLog) {
    $lastLine = Get-Content $todayLog.FullName -Tail 1
    # 日志行格式：2026-09-10 08:24:58,410 [INFO] ...
    if ($lastLine -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
        $lastTs = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
    }
    Write-Host ("Log File   : {0}" -f $todayLog.Name) -ForegroundColor Yellow
    Write-Host ("File mtime : {0}" -f $todayLog.LastWriteTime.ToString('HH:mm:ss')) -ForegroundColor Gray
    if ($lastTs) {
        $gap = ($now - $lastTs)
        Write-Host ("Log content: {0}" -f $lastTs.ToString('HH:mm:ss')) -ForegroundColor Yellow
        Write-Host ("Gap        : {0} min {1} sec" -f $gap.Minutes, $gap.Seconds) -ForegroundColor Yellow
    } else {
        Write-Host 'Cannot parse timestamp from last line' -ForegroundColor Gray
    }
} else {
    Write-Host '[DEAD] no live_*.log found' -ForegroundColor Red
}
Write-Host ''

# 4. 结论（用日志内容时间判断，容差 5 分钟）
Write-Host '==================== Verdict ====================' -ForegroundColor Cyan
if ($proc -and $lastTs -and ((($now - $lastTs).TotalMinutes) -lt 5)) {
    Write-Host 'GOOD: process alive, log fresh. Fix works.' -ForegroundColor Green
} elseif ($proc) {
    Write-Host 'WARN: process alive but log content stale > 5 min.' -ForegroundColor Yellow
} else {
    Write-Host 'BAD: process dead. Note time, check LastResult (1067?).' -ForegroundColor Red
}
Write-Host ''