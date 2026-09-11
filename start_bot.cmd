@echo off
rem 啟動 clusdt 原油情報機器人（循環模式）
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
%PY% main.py --loop --interval 300
pause
