@echo off
rem ZJU bridge launcher - finds the bridge folder regardless of codepage
set "BRDIR="
for /d %%D in ("%~dp004-*") do set "BRDIR=%%D\zju-bridge"
if "%BRDIR%"=="" (
  echo [X] bridge folder not found
  pause
  exit /b 1
)
python -c "print(1)" >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Install it first:
  echo     winget install Python.Python.3.12
  pause
  exit /b 1
)
echo Starting ZJU bridge ... keep this window open while using the workbench.
python "%BRDIR%\zju_bridge.py" %*
if errorlevel 1 pause
