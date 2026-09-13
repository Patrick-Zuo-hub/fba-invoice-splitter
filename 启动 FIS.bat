@echo off
chcp 65001 >nul
setlocal

set "FIS_ROOT=%~dp0"
set "FIS_PYTHON=%FIS_ROOT%.venv\Scripts\python.exe"

if not exist "%FIS_PYTHON%" goto :missing_venv

set "PYTHONPATH=%FIS_ROOT%src"
cd /d "%FIS_ROOT%." || exit /b 1
"%FIS_PYTHON%" -m fis
exit /b %ERRORLEVEL%

:missing_venv
echo FIS 启动失败：未找到项目运行环境 .venv\Scripts\python.exe。
echo 请先完成开发环境配置，再重新双击此文件。
pause
exit /b 1
