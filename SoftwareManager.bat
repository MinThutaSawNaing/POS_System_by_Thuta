@echo off
setlocal enableextensions
title Parrot POS Management Tool
cd /d "%~dp0"

:MENU
cls
echo ===================================================
echo              PARROT POS SYSTEM MANAGER
echo ===================================================
echo  1. Start the Server
echo  2. Stop the Server
echo  3. First-Time Setup (New Windows PCs)
echo  4. Update Software (Git Pull + Restart)
echo  5. Uninstall/Delete POS Software
echo  6. Exit
echo ===================================================
set /p choice="Enter your choice (1-6): "

if "%choice%"=="1" goto START_SERVER
if "%choice%"=="2" goto STOP_SERVER
if "%choice%"=="3" goto SETUP
if "%choice%"=="4" goto UPDATE
if "%choice%"=="5" goto UNINSTALL
if "%choice%"=="6" exit
goto MENU

:START_SERVER
echo Starting Parrot POS...
start "ParrotPOS_Server" /min ".venv\Scripts\python.exe" app.py
echo Server is running in the background.
pause
goto MENU

:STOP_SERVER
echo Stopping Parrot POS...
:: This finds the process running app.py and kills it
taskkill /FI "WINDOWTITLE eq ParrotPOS_Server*" /F
echo Server stopped.
pause
goto MENU

:SETUP
echo Running First-Time Setup...
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python and add to PATH.
    pause
    goto MENU
)
if not exist ".venv" (
    python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\pip.exe" install -r requirements.txt

:: Shortcut Creation logic
set "S_PATH=%USERPROFILE%\Desktop\Parrot POS.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%S_PATH%');" ^
  "$s.TargetPath='%CD%\.venv\Scripts\python.exe';" ^
  "$s.Arguments='%CD%\app.py';" ^
  "$s.WorkingDirectory='%CD%';" ^
  "$s.Save()"
echo Setup Complete!
pause
goto MENU

:UPDATE
echo Updating software...
git pull origin main
echo Restarting server...
taskkill /FI "WINDOWTITLE eq ParrotPOS_Server*" /F >nul 2>&1
start "ParrotPOS_Server" /min ".venv\Scripts\python.exe" app.py
echo Update applied and server restarted.
pause
goto MENU

:UNINSTALL
set /p confirm="Are you SURE you want to delete everything? (Y/N): "
if /I "%confirm%" NEQ "Y" goto MENU
echo Cleaning up...
taskkill /FI "WINDOWTITLE eq ParrotPOS_Server*" /F >nul 2>&1
cd ..
rd /s /q "%~dp0"
echo POS Software has been removed. 
pause
exit