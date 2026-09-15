#!/bin/bash
# Bundle the SPM binary into a proper macOS .app bundle and install to /Applications
set -e

BINARY=".build/release/DoGoodFactory"

# Always rebuild
echo "Building release binary..."
swift build -c release

# Kill existing instance if running
pkill -f "DoGoodFactory" 2>/dev/null || true
sleep 1

# Install as "Do Good Factory.app"
APP_NAME="Do Good Factory"
BUNDLE_DIR="$APP_NAME.app"
INSTALL_DIR="/Applications/$BUNDLE_DIR"

# Create bundle structure
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/Contents/MacOS"
mkdir -p "$BUNDLE_DIR/Contents/Resources"

# Copy binary
cp "$BINARY" "$BUNDLE_DIR/Contents/MacOS/DoGoodFactory"

# Create Info.plist
cat > "$BUNDLE_DIR/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Do Good Factory</string>
    <key>CFBundleDisplayName</key>
    <string>Do Good Factory</string>
    <key>CFBundleIdentifier</key>
    <string>com.batesai.dogood-factory</string>
    <key>CFBundleVersion</key>
    <string>2.1</string>
    <key>CFBundleShortVersionString</key>
    <string>2.1</string>
    <key>CFBundleExecutable</key>
    <string>DoGoodFactory</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# Install to /Applications
rm -rf "$INSTALL_DIR"
cp -R "$BUNDLE_DIR" "$INSTALL_DIR"

echo "Installed: $INSTALL_DIR"
echo "Run with: open '/Applications/$BUNDLE_DIR'"

# Clean up local bundle
rm -rf "$BUNDLE_DIR"
