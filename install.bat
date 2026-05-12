@echo off
echo Setting up screencast-to-figma...

python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Install from https://python.org
    pause
    exit /b 1
)

echo Creating virtual environment...
python -m venv venv
call venv\Scripts\activate

echo Installing dependencies...
pip install --upgrade pip -q
pip install -r requirements.txt

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Warning: ffmpeg not found. Download from https://ffmpeg.org/download.html and add to PATH.
)

echo.
echo Done. Start the server:
echo   venv\Scripts\activate ^&^& python app.py
pause
