@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run setup.bat first. Python 3.10 or newer is required.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m ila_viewer %*
