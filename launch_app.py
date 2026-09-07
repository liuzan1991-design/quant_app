# -*- coding: utf-8 -*-
"""后台启动 Streamlit 界面（双击本文件即可打开浏览器界面）。"""
import os
import subprocess
import sys
import webbrowser
import time

APP_DIR = r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app"
URL = "http://localhost:8501"
LOG_PATH = os.path.join(APP_DIR, "logs", "launch.log")

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
log_fh = open(LOG_PATH, "ab", buffering=0)
log_fh.write(("\n===== launch %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))

proc = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", "app.py",
     "--server.headless", "true", "--server.port", "8501",
     "--browser.gatherUsageStats", "false"],
    cwd=APP_DIR,
    stdin=subprocess.DEVNULL,
    stdout=log_fh,
    stderr=log_fh,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
)

# 等待服务就绪后自动打开浏览器
import urllib.request
for _ in range(20):
    time.sleep(1.5)
    try:
        urllib.request.urlopen(URL, timeout=3)
        break
    except Exception:
        continue
webbrowser.open(URL)
try:
    print("界面已启动:", URL)
except Exception:
    pass
