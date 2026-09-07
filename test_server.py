# -*- coding: utf-8 -*-
"""真实启动 Streamlit 服务器并验证 HTTP 响应。"""
import subprocess
import sys
import time
import urllib.request

APP_DIR = r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app"
p = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", "app.py",
     "--server.headless", "true", "--server.port", "8510"],
    cwd=APP_DIR,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    ok = False
    for _ in range(20):
        time.sleep(1.5)
        try:
            with urllib.request.urlopen("http://localhost:8510", timeout=5) as r:
                print("HTTP", r.status)
                ok = True
                break
        except Exception:
            continue
    print("服务器响应:", "OK" if ok else "FAILED")
    assert ok
finally:
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
