# -*- coding: utf-8 -*-
"""真实启动 Streamlit 服务器并验证 HTTP 响应。

⚠️ 本文件原先把逻辑写在**模块级** —— pytest 收集阶段一 import 本文件，
就会启动 Streamlit 服务器（端口 8510）并空等 30 秒，污染整个收集过程。
现已重构为函数，仅在测试/显式调用时执行；标记 slow。
"""
import subprocess
import sys
import time
import urllib.request

import pytest

APP_DIR = r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app"


def main() -> bool:
    """启动真实 Streamlit 服务器并验证 HTTP 响应，返回是否成功。

    起服务 + 最多等 30 秒 + 收尾 terminate，全过程在函数内，
    不再有 import 副作用。
    """
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
        return ok
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


@pytest.mark.slow
def test_server_starts():
    """pytest 入口：真实起服务验证 HTTP 响应（30+ 秒，进慢档）。"""
    assert main()


if __name__ == "__main__":
    main()
