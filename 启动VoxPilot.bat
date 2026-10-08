@echo off
chcp 936 >nul
title VoxPilot 口语自动训练
cd /d "%~dp0"
echo ======================================
echo   VoxPilot 正在启动，请稍候...
echo   （出现登录窗口后，在里面输入账号密码并点“开始运行”）
echo ======================================
"%~dp0.venv\Scripts\python.exe" "%~dp0src\main.py"
echo.
echo 程序已退出。如果上面有红色报错，请截图发给助手。
pause
