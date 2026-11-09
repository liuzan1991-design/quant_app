# -*- coding: utf-8 -*-
"""QMT 策略心跳超时 → 邮件告警（在 QMT 外部运行，不受沙箱限制）。

为什么放外面：2026-11-24（或 11-14，日期待确认）起 QMT 内置策略禁止
文件读写与任何网络访问，策略内部**不可能**发告警。可观测性只能靠外部
读 QMT 日志。

判据（与 scripts/qmt_hb_watch.py 一致）：
  读取 <安装>/userdata/log/XtClient_FormulaOutput_YYYYMMDD.log
  中最后一条 [RO] 行的时间戳，超过 --stale-min 分钟即判为停摆。

行为：
  - 只在 A 股交易时段（北京 09:30–15:00）告警，盘后不吵
  - 冷却 --cooldown-min 分钟，避免连环轰炸
  - 未配置 SMTP 时不静默失败：写本地告警文件 + 非零退出码
  - 邮件发送失败同样落本地文件

配置文件 data/alert_config.json（已 .gitignore 掉，不入库）：
  {
    "enabled": true,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_user": "you@qq.com",
    "smtp_pass": "授权码，不是登录密码",
    "mail_to": ["you@qq.com"]
  }

用法：
  python scripts/qmt_alert_watch.py --test          # 发一封测试邮件
  python scripts/qmt_alert_watch.py                 # 检查一次
  python scripts/qmt_alert_watch.py --loop 300      # 每 5 分钟检查（常驻）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import smtplib
import sys
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = Path("D:/迅投QMT交易终端   国盛证券实盘版/userdata/log")
CONFIG = APP_DIR / "data" / "alert_config.json"
OUT_DIR = Path(r"D:\Codex输出\qmt_alerts")
STATE = OUT_DIR / "alert_state.json"
BJ = dt.timezone(dt.timedelta(hours=8))


def bj_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(BJ)


def log(msg: str) -> None:
    line = "[%s] %s" % (bj_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / ("watch_%s.log" % bj_now().strftime("%Y%m%d"))).open(
                "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def in_session(now: dt.datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    # 午休 11:30–13:00 没有 tick ⇒ 策略不产出 [RO] 行，判活无意义。
    # 2026-11-09 误报：11:29 是上午末根，12:35 按「66 分钟无输出」发了心跳超时邮件。
    # 13:00 后再给 5 分钟宽限，等复市第一根 bar 落地才判。
    if (11 * 60 + 30) <= hm < (13 * 60 + 5):
        return False
    return (9 * 60 + 30) <= hm <= (15 * 60)


def last_output_age() -> tuple[float | None, str]:
    """返回 (最后一条 [RO] 行距今分钟数, 该行内容)。"""
    lp = LOG_DIR / ("XtClient_FormulaOutput_%s.log" % bj_now().strftime("%Y%m%d"))
    if not lp.exists():
        return None, ""
    last_ts = None
    last_txt = ""
    try:
        with lp.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[RO]" not in line:
                    continue
                head = line[:19]
                try:
                    last_ts = dt.datetime.strptime(head, "%Y-%m-%d %H:%M:%S")
                    last_txt = line.rstrip()
                except ValueError:
                    continue
    except Exception as e:
        log("读日志失败：%s" % e)
        return None, ""
    if last_ts is None:
        return None, ""
    now_local = dt.datetime.now()
    return (now_local - last_ts).total_seconds() / 60.0, last_txt


def send_mail(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "config.enabled != true"
    need = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "mail_to")
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        return False, "缺少配置项: %s" % ",".join(missing)
    to_list = cfg["mail_to"]
    if isinstance(to_list, str):
        to_list = [to_list]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("QMT 监控", cfg["smtp_user"]))
    msg["To"] = ",".join(to_list)
    try:
        port = int(cfg["smtp_port"])
        if port == 465:
            s = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=20)
        else:
            s = smtplib.SMTP(cfg["smtp_host"], port, timeout=20)
            s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.sendmail(cfg["smtp_user"], to_list, msg.as_string())
        s.quit()
        return True, "已发送至 %s" % ",".join(to_list)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def cooldown_ok(minutes: int) -> bool:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        last = dt.datetime.fromisoformat(st["last_alert"])
    except Exception:
        return True
    return (bj_now().replace(tzinfo=None) - last).total_seconds() / 60.0 >= minutes


def mark_alert() -> None:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_alert": bj_now().replace(tzinfo=None).isoformat()}),
                         encoding="utf-8")
    except Exception:
        pass


def write_local(subject: str, body: str) -> Path | None:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        p = OUT_DIR / ("ALERT_%s.txt" % bj_now().strftime("%Y%m%d_%H%M%S"))
        p.write_text(subject + "\n\n" + body, encoding="utf-8")
        return p
    except Exception:
        return None


def check_once(stale_min: int, cooldown_min: int, cfg: dict) -> int:
    now = bj_now()
    age, txt = last_output_age()

    if age is None:
        log("今日日志无 [RO] 行（策略可能未启动）| 北京 %s"
            % now.strftime("%Y-%m-%d %H:%M:%S"))
        if not in_session(now):
            return 0
        subject = "[QMT] 策略无输出（今日无记录）"
        body = "北京 %s\n今日 FormulaOutput 日志中没有 [RO] 行。\n策略可能未启动或日志被清。" % now
    elif age > stale_min and in_session(now):
        log("STALE %.1f 分钟 | 北京 %s" % (age, now.strftime("%H:%M:%S")))
        subject = "[QMT] 策略心跳超时 %.0f 分钟" % age
        body = ("北京 %s\n最后一条输出在 %.1f 分钟前。\n\n最后一行:\n%s\n"
                "\n可能原因：QMT 客户端退出 / 网络中断 / 策略被停止。"
                % (now.strftime("%Y-%m-%d %H:%M:%S"), age, txt[-300:]))
    else:
        log("OK 最后输出 %.1f 分钟前（判活窗口外不告警：午休/开收盘边界）" % age
            if not in_session(now) else "OK 最后输出 %.1f 分钟前" % age)
        return 0

    if not cooldown_ok(cooldown_min):
        log("冷却期内，跳过发送（--cooldown-min %d）" % cooldown_min)
        return 0

    ok, detail = send_mail(cfg, subject, body)
    if ok:
        log("邮件告警成功：%s" % detail)
        mark_alert()
        return 0
    p = write_local(subject, body)
    log("邮件告警失败（%s）；已写本地文件：%s" % (detail, p))
    mark_alert()
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-min", type=int, default=5)
    ap.add_argument("--cooldown-min", type=int, default=60)
    ap.add_argument("--loop", type=int, default=0, help="秒；0=只检查一次")
    ap.add_argument("--test", action="store_true", help="发一封测试邮件后退出")
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.test:
        ok, detail = send_mail(cfg, "[QMT] 邮件告警测试",
                               "这是一封测试邮件。收到即说明告警通道可用。\n"
                               "北京 %s" % bj_now().strftime("%Y-%m-%d %H:%M:%S"))
        log("测试发送：%s | %s" % (ok, detail))
        return 0 if ok else 1

    if not args.loop:
        return check_once(args.stale_min, args.cooldown_min, cfg)

    log("常驻模式启动：每 %d 秒检查，阈值 %d 分钟，冷却 %d 分钟"
        % (args.loop, args.stale_min, args.cooldown_min))
    if not cfg.get("enabled"):
        log("警告：未配置 data/alert_config.json 或 enabled=false ⇒ 告警只会写本地文件")
    while True:
        try:
            check_once(args.stale_min, args.cooldown_min, cfg)
        except Exception as e:
            log("检查异常：%s: %s" % (type(e).__name__, e))
        time.sleep(max(60, args.loop))


if __name__ == "__main__":
    sys.exit(main())
