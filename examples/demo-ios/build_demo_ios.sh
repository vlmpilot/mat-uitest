#!/usr/bin/env bash
# 编译 iOS 模拟器版演示靶场：不建 Xcode 工程（pbxproj 手写脆弱），
# swiftc 直出二进制 + 手工组装 .app（模拟器不强制签名，ad-hoc 兜底）。
# 用法：bash build_demo_ios.sh
#      xcrun simctl install booted build/MatDemo.app
#      xcrun simctl launch booted com.mat.demo
set -euo pipefail
cd "$(dirname "$0")"

SDK=$(xcrun -sdk iphonesimulator -show-sdk-path)
ARCH=$(uname -m)   # 本机 x86_64；Apple Silicon 上自动 arm64
mkdir -p build

echo "== swiftc 编译（${ARCH}-apple-ios16.0-simulator）=="
xcrun -sdk iphonesimulator swiftc \
  -parse-as-library \
  -target "${ARCH}-apple-ios16.0-simulator" \
  app.swift -o build/MatDemo

echo "== 组装 .app =="
mkdir -p build/MatDemo.app
cp build/MatDemo build/MatDemo.app/MatDemo
cat > build/MatDemo.app/Info.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>MatDemo</string>
  <key>CFBundleIdentifier</key><string>com.mat.demo</string>
  <key>CFBundleName</key><string>MatDemo</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSRequiresIPhoneOS</key><true/>
  <key>MinimumOSVersion</key><string>16.0</string>
  <key>UIDeviceFamily</key><array><integer>1</integer></array>
  <key>CFBundleSupportedPlatforms</key><array><string>iPhoneSimulator</string></array>
</dict></plist>
codesign -s - --force build/MatDemo.app 2>/dev/null || true

echo "== 完成 =="
echo "安装：xcrun simctl install booted build/MatDemo.app && xcrun simctl launch booted com.mat.demo"
