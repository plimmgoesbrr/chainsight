@echo off
setlocal
cd /d "%~dp0"
echo.
echo  ===  ChainSight setup  (one time, needs internet)  ===
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo  Python was not found.
  echo  Install Python 3.11 or 3.12 from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during install, then run setup.bat again.
  pause
  exit /b 1
)
%PY% --version
if not exist ".venv\Scripts\python.exe" (
  echo  Creating virtual environment ...
  %PY% -m venv .venv || goto :err
)
echo  Installing packages (takes 1-3 minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
echo  Preparing data, model and first analysis ...
".venv\Scripts\python.exe" -m chainsight.cli prepare || goto :err
echo.
echo  Setup complete. Double-click run.bat to start ChainSight.
echo.
pause
exit /b 0
:err
echo.
echo  Setup failed - read the message above. See README.md, section Troubleshooting.
pause
exit /b 1
