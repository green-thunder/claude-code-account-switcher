#!/bin/sh
# Builds "CCS Usage.app" into ~/Applications. Needs only the Xcode command line tools (swiftc).
set -e
cd "$(dirname "$0")"
app="$HOME/Applications/CCS Usage.app"
mkdir -p "$app/Contents/MacOS"
cp Info.plist "$app/Contents/Info.plist"
swiftc -O -parse-as-library -swift-version 5 -target "$(uname -m)-apple-macos14" CCSUsage.swift -o "$app/Contents/MacOS/CCSUsage"
echo "built $app"
