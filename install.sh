#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

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

# Check ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo ""
    echo "Warning: ffmpeg not found. Install it before using the plugin:"
    echo "  macOS:  brew install ffmpeg"
    echo "  Linux:  sudo apt install ffmpeg"
    echo ""
fi

# Create venv and install deps
echo "Creating virtual environment..."
python3 -m venv "$REPO_DIR/venv"
"$REPO_DIR/venv/bin/pip" install --upgrade pip -q
"$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

# Register as login service (macOS)
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST="$HOME/Library/LaunchAgents/com.screencast-to-figma.plist"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.screencast-to-figma</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_DIR/venv/bin/python</string>
        <string>$REPO_DIR/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/screencast-to-figma.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/screencast-to-figma.log</string>
</dict>
</plist>
EOF
    launchctl load "$PLIST"
    echo "Server registered as a login service — starts automatically on login."

# Register as login service (Linux systemd)
elif command -v systemctl &> /dev/null; then
    SERVICE="$HOME/.config/systemd/user/screencast-to-figma.service"
    mkdir -p "$(dirname "$SERVICE")"
    cat > "$SERVICE" <<EOF
[Unit]
Description=Screencast to Figma server

[Service]
ExecStart=$REPO_DIR/venv/bin/python $REPO_DIR/app.py
WorkingDirectory=$REPO_DIR
Restart=on-failure

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable screencast-to-figma
    systemctl --user start screencast-to-figma
    echo "Server registered as a systemd user service — starts automatically on login."
fi

echo ""
echo "Done. The server runs in the background — just open Figma and use the plugin."
