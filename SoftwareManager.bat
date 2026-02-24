@echo off
setlocal enableextensions enabledelayedexpansion
title Parrot POS Management Tool
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
set "APP_FILE=%CD%\app.py"
set "VENV_DIR=%CD%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "PIP_EXE=%VENV_DIR%\Scripts\pip.exe"
set "SHORTCUT_PATH=%USERPROFILE%\Desktop\Parrot POS.lnk"

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
set "choice="
set /p choice="Enter your choice (1-6): "
if not defined choice goto MENU
for /f "delims=0123456789" %%A in ("%choice%") do goto MENU

if "%choice%"=="1" goto START_SERVER
if "%choice%"=="2" goto STOP_SERVER
if "%choice%"=="3" goto SETUP
if "%choice%"=="4" goto UPDATE
if "%choice%"=="5" goto UNINSTALL
if "%choice%"=="6" exit
goto MENU

:START_SERVER
call :REQUIRE_FILE "%APP_FILE%" "app.py not found. Please run setup or fix the installation."
call :REQUIRE_FILE "%PYTHON_EXE%" "Virtual environment not found. Please run setup (option 3)."
call :IS_RUNNING
if not errorlevel 1 (
    echo Server is already running.
    pause
    goto MENU
)
echo Starting Parrot POS...
start "ParrotPOS_Server" /min "%PYTHON_EXE%" "%APP_FILE%"
echo Server is running in the background.
pause
goto MENU

:STOP_SERVER
echo Stopping Parrot POS...
call :STOP_PROCESS
if errorlevel 1 (
    echo No running server process was found.
) else (
    echo Server stopped.
)
pause
goto MENU

:SETUP
echo Running First-Time Setup...
call :REQUIRE_CMD python "Python not found. Please install Python and add it to PATH."
call :REQUIRE_FILE "%APP_FILE%" "app.py not found. Please make sure you are in the POS folder."
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python and add to PATH.
    pause
    goto MENU
)
if not exist "%VENV_DIR%" (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        goto MENU
    )
)
call :REQUIRE_FILE "%PIP_EXE%" "pip not found in virtual environment."
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip.
    pause
    goto MENU
)
call :REQUIRE_FILE "%CD%\requirements.txt" "requirements.txt not found."
"%PIP_EXE%" install -r "%CD%\requirements.txt"
if errorlevel 1 (
    echo ERROR: Failed to install requirements.
    pause
    goto MENU
)

:: Shortcut Creation logic
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT_PATH%');" ^
  "$s.TargetPath='%PYTHON_EXE%';" ^
  "$s.Arguments='%APP_FILE%';" ^
  "$s.WorkingDirectory='%CD%';" ^
  "$s.Save()"
echo Setup Complete!
pause
goto MENU

:UPDATE
echo Updating software...
if not exist "%CD%\.git" (
    echo ERROR: This folder is not a git repository.
    pause
    goto MENU
)
call :REQUIRE_CMD git "Git not found. Please install Git or update manually."
git pull origin main
if errorlevel 1 (
    echo ERROR: Git pull failed. Resolve the issue and try again.
    pause
    goto MENU
)
echo Restarting server...
call :STOP_PROCESS >nul 2>&1
call :REQUIRE_FILE "%PYTHON_EXE%" "Virtual environment not found. Run setup first."
start "ParrotPOS_Server" /min "%PYTHON_EXE%" "%APP_FILE%"
echo Update applied and server restarted.
pause
goto MENU

:UNINSTALL
echo WARNING: This will delete the POS folder at:
echo   %CD%
echo This cannot be undone.
choice /c YN /m "Are you sure you want to continue?"
if errorlevel 2 goto MENU
set /p confirm="Type DELETE to confirm: "
if /I not "%confirm%"=="DELETE" goto MENU
echo Cleaning up...
call :STOP_PROCESS >nul 2>&1
cd /d "%SCRIPT_DIR%"
if "%SCRIPT_DIR%"=="" (
    echo ERROR: Invalid script directory. Aborting.
    pause
    goto MENU
)
cd /d "%SCRIPT_DIR%\.."
rd /s /q "%SCRIPT_DIR%"
echo POS Software has been removed. 
pause
exit

:: ===== Helper functions =====
:REQUIRE_FILE
if not exist "%~1" (
    echo ERROR: %~2
    pause
    goto MENU
)
exit /b 0

:REQUIRE_CMD
where %~1 >nul 2>nul
if errorlevel 1 (
    echo ERROR: %~2
    pause
    goto MENU
)
exit /b 0

:IS_RUNNING
powershell -NoProfile -Command ^
  "$path = [IO.Path]::GetFullPath('%APP_FILE%');" ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like ('*' + $path + '*') };" ^
  "if ($procs) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

:STOP_PROCESS
powershell -NoProfile -Command ^
  "$path = [IO.Path]::GetFullPath('%APP_FILE%');" ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like ('*' + $path + '*') };" ^
  "if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; exit 0 } else { exit 1 }"
exit /b %errorlevel%
