@echo off
REM Collaborative Whiteboard - Windows launcher (double-click, or run from a terminal)
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python with Tk was not found. Install Python from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)
if not "%~1"=="" (
  echo Checking connection to %~1 ...
  %PY% tools\netcheck.py %~1
)
%PY% -m client.main %*
if errorlevel 1 pause
