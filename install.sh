#!/bin/bash
set -e

echo "Setting up screencast-to-figma..."

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Install from https://python.org"
    exit 1
fi

PYVER=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PYVER" -lt 10 ]; then
    echo "Error: Python 3.10+ required. Current: $(python3 --version)"
    exit 1
fi

# Create venv and install
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt

# Check ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo ""
    echo "Warning: ffmpeg not found. Install it before running the server:"
    echo "  macOS:  brew install ffmpeg"
    echo "  Linux:  sudo apt install ffmpeg"
fi

echo ""
echo "Done. Start the server:"
echo "  source venv/bin/activate && python app.py"
