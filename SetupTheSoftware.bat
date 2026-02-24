@echo off
setlocal enableextensions

cd /d "%~dp0"

echo === Parrot POS Windows Setup ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python was not found in PATH.
  echo Install Python 3.x and ensure it is added to PATH, then re-run this script.
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    echo.
    pause
    exit /b 1
  )
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo ERROR: Failed to upgrade pip.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 (
  echo ERROR: Failed to install dependencies.
  echo.
  pause
  exit /b 1
)

echo.
echo Setup complete.
echo To run the app:
echo   .\.venv\Scripts\python.exe app.py
echo.
pause
endlocal