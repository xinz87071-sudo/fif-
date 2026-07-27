# VoxPilot 启动脚本
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "未找到 .venv，请先创建虚拟环境并安装依赖。"
}

& ".\.venv\Scripts\python.exe" "src\main.py"
