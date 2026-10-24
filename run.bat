@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo  Please run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m chainsight.cli prepare
echo  Opening the dashboard in your browser ...
".venv\Scripts\python.exe" -m chainsight.cli serve
pause
