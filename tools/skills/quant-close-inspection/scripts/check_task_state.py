# -*- coding: utf-8 -*-
"""只读：查 Windows 计划任务"最近事件 / 是否仍在运行"。

用法: D:/Anaconda3/python.exe check_task_state.py [任务名, 默认 QuantLiveRun]
原理: 读 TaskScheduler 操作日志（需 pywin32，Anaconda 自带）。
用途: 本机三条常规路径均堵死时（schtasks 被黑名单 / 终端工具返回值假绿 /
      wevtutil 报参数错误）作为替代取证手段。
"""
import re, sys, datetime
import win32evtlog

TASK = sys.argv[1] if len(sys.argv) > 1 else "QuantLiveRun"
CH = r"Microsoft-Windows-TaskScheduler/Operational"
EV = {100: "TaskStart", 101: "TaskStartFailed", 102: "TaskCompleted(OK)",
      103: "TaskCompleted(FAIL)", 111: "TaskTerminated(byUser)", 129: "CreatedTaskProcess",
      200: "ActionStart", 201: "ActionCompleted", 202: "ActionFail",
      203: "ActionTerminated(byUser)", 107: "TimeTrigger", 108: "RegTrigger"}

q = win32evtlog.EvtQuery(CH, win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection, None)
rows, scanned = [], 0
while scanned < 6000:
    evs = win32evtlog.EvtNext(q, 50)
    if not evs:
        break
    for ev in evs:
        scanned += 1
        x = win32evtlog.EvtRender(ev, win32evtlog.EvtRenderEventXml)
        nm = re.search(r"<Data Name='TaskName'>([^<]+)", x)
        if not nm or TASK not in nm.group(1):
            continue
        t = re.search(r"SystemTime='([^']+)'", x)
        eid = re.search(r"<EventID>(\d+)</EventID>", x)
        rc = re.search(r"<Data Name='ResultCode'>(\d+)", x)
        utc = datetime.datetime.fromisoformat(t.group(1).replace("Z", "+00:00"))
        loc = utc.astimezone(datetime.timezone(datetime.timedelta(hours=3)))
        rows.append((loc.strftime("%m-%d %H:%M:%S"), int(eid.group(1)),
                     EV.get(int(eid.group(1)), "?"),
                     (f"rc={int(rc.group(1))}(0x{int(rc.group(1)):08X})" if rc else "")))

print(f"[{TASK}] 扫描 {scanned} 条事件，命中 {len(rows)} 条（本机 +03:00，新 -> 旧）")
for r in rows[:12]:
    print(f"  {r[0]}   EID={r[1]:<4} {r[2]:<26} {r[3]}")

if rows:
    s = next((i for i, r in enumerate(rows) if r[1] in (100, 129)), None)
    d = next((i for i, r in enumerate(rows) if r[1] in (102, 103, 111)), None)
    if s is not None and (d is None or s < d):
        print("  => 判定: [疑仍在运行] 有新 Start 且无更新的 Completed/Terminated")
    else:
        print("  => 判定: [已结束/Ready] 最近的 Completed-Terminated 晚于最近的 Start")
