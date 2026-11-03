#!/bin/bash
# Installs or removes the weekly P2 Radar check (Mondays at 10:00), which posts a
# notification when projects have work that isn't backed up.
#   ./weekly.sh install     ./weekly.sh uninstall     ./weekly.sh run (once, now)
set -euo pipefail

LABEL="com.udo.project-radar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_BIN="$HOME/Applications/P2 Radar.app/Contents/MacOS/Radar"
LOG_DIR="$HOME/Library/Application Support/Project Radar"

case "${1:-}" in
  install)
    [[ -x "$APP_BIN" ]] || { echo "Build and install P2 Radar.app first: ./build.sh"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$APP_BIN</string><string>--notify</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/weekly.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/weekly.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Weekly check installed (Mondays 10:00). Log: $LOG_DIR/weekly.log"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Weekly check removed."
    ;;
  run)
    launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "Started. Check $LOG_DIR/weekly.log"
    ;;
  *)
    echo "usage: $0 install | uninstall | run"; exit 1 ;;
esac
