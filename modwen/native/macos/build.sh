#!/usr/bin/env bash
# Build the macOS system-audio helper (Core Audio process tap; macOS 14.2+, Xcode Command Line Tools).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
swiftc -O -target "$(uname -m)-apple-macos14.2" \
  -framework CoreAudio -framework AudioToolbox \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist \
  -o build/modwen-tap modwen-tap.swift
echo "Built $(pwd)/build/modwen-tap"
