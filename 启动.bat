@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在启动量化可视化回测平台...
python -m streamlit run app.py
pause
