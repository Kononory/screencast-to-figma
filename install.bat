@echo off
setlocal

set REPO_DIR=%~dp0

echo Setting up screencast-to-figma...

python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Install from https://python.org
    pause
    exit /b 1
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Warning: ffmpeg not found. Download from https://ffmpeg.org/download.html and add to PATH.
    echo.
)

echo Creating virtual environment...
python -m venv "%REPO_DIR%venv"
call "%REPO_DIR%venv\Scripts\activate"

echo Installing dependencies...
pip install --upgrade pip -q
pip install -r "%REPO_DIR%requirements.txt"

:: Create a startup script that runs silently
set STARTUP_SCRIPT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\screencast-to-figma.vbs
echo Set WshShell = CreateObject("WScript.Shell") > "%STARTUP_SCRIPT%"
echo WshShell.Run """"%REPO_DIR%venv\Scripts\pythonw.exe"""" """"%REPO_DIR%app.py"""", 0, False >> "%STARTUP_SCRIPT%"

:: Start the server now
start "" /B "%REPO_DIR%venv\Scripts\pythonw.exe" "%REPO_DIR%app.py"

echo.
echo Done. The server runs in the background — starts automatically on login.
echo Just open Figma and use the plugin.
pause
