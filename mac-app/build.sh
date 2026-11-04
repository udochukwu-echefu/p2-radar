#!/bin/bash
# Builds P2 Radar.app from main.swift + the Python engine in ../project-radar,
# signs it ad hoc and installs it to ~/Applications.
#   ./build.sh                build and install the app, and sync the engine
#   ./build.sh --engine       only sync the engine + dashboard and relaunch P2 Radar
#                             (no re-sign, so macOS won't ask for folder access again)
#   ./build.sh --no-install   build only (into ./build)
# RADAR_ARCHS="arm64 x86_64" builds a universal app; RADAR_VERSION sets its version.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$HERE/../project-radar"
BUILD="$HERE/build"
APP="$BUILD/P2 Radar.app"
DEST="$HOME/Applications/P2 Radar.app"
OLD="$HOME/Applications/Radar.app"   # the app's name before it became P2 Radar
SYNCED="$HOME/Library/Application Support/Project Radar/engine"

sync_engine() {
  mkdir -p "$SYNCED"
  cp "$ENGINE"/*.py "$ENGINE/dashboard.html" "$SYNCED/"
  echo "Synced engine to $SYNCED"
}

if [[ "${1:-}" == "--engine" ]]; then
  sync_engine
  if pgrep -xq Radar; then
    osascript -e 'tell application id "com.udo.radar" to quit' || true
    sleep 1.5
    open "$DEST"
    echo "Relaunched P2 Radar"
  fi
  exit 0
fi

rm -rf "$BUILD" && mkdir -p "$BUILD"

echo "→ icon"
swift "$HERE/make-icon.swift" "$BUILD/icon-1024.png" >/dev/null
ICONSET="$BUILD/Radar.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$BUILD/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/Radar.icns"

echo "→ compile"
slices=()
archflags=()
for arch in ${RADAR_ARCHS:-$(uname -m)}; do
  swiftc -O -target "$arch-apple-macos14.0" "$HERE/main.swift" "$HERE/MagSafe.swift" -o "$BUILD/Radar-$arch"
  slices+=("$BUILD/Radar-$arch")
  archflags+=(-arch "$arch")
done
lipo -create "${slices[@]}" -output "$BUILD/Radar"
# The MagSafe light helper: reads the light as the user; the root daemon copy runs from
# /Library/PrivilegedHelperTools once the user sets it up from the menu bar.
clang -O2 -Wall -Wextra "${archflags[@]}" -mmacosx-version-min=14.0 -framework IOKit -framework CoreFoundation \
  -o "$BUILD/magsafe-led" "$HERE/magsafe-led.c"

echo "→ bundle"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/radar"
cp "$BUILD/Radar" "$APP/Contents/MacOS/Radar"
cp "$BUILD/Radar.icns" "$APP/Contents/Resources/Radar.icns"
cp "$BUILD/magsafe-led" "$APP/Contents/Resources/magsafe-led"
cp "$ENGINE"/*.py "$ENGINE/dashboard.html" "$APP/Contents/Resources/radar/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.udo.radar</string>
  <key>CFBundleName</key><string>P2 Radar</string>
  <key>CFBundleDisplayName</key><string>P2 Radar</string>
  <key>CFBundleExecutable</key><string>Radar</string>
  <key>CFBundleIconFile</key><string>Radar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Finds work that isn't backed up and clutter in Downloads.</string>
  <key>NSDesktopFolderUsageDescription</key><string>P2 Radar looks for git projects on your Desktop to check whether their work is backed up.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>P2 Radar looks for git projects in Documents to check whether their work is backed up.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>P2 Radar sorts your Downloads so you can choose what to move to the Trash.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST
plutil -replace CFBundleShortVersionString -string "${RADAR_VERSION:-1.0}" "$APP/Contents/Info.plist"

# A stable signing identity keeps macOS's folder permissions across rebuilds.
# Create one once in Keychain Access (see README); otherwise sign ad hoc.
IDENTITY="${RADAR_SIGN_ID:-Radar Local Signing}"
if security find-identity -p codesigning 2>/dev/null | grep -q "\"$IDENTITY\""; then
  echo "→ sign ($IDENTITY)"
  codesign --force --deep --sign "$IDENTITY" "$APP"
else
  echo "→ sign (ad hoc)"
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP"

if [[ "${1:-}" == "--no-install" ]]; then
  echo "Built $APP"
  exit 0
fi

echo "→ install"
if pgrep -xq Radar; then osascript -e 'tell application id "com.udo.radar" to quit' || true; sleep 1; fi
mkdir -p "$HOME/Applications"
rm -rf "$DEST" "$OLD"
ditto "$APP" "$DEST"
sync_engine
echo "Installed $DEST"
