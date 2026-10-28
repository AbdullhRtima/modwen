#!/usr/bin/env bash
# Stage what the desktop app bundles: the engine's committed source + built web UI (+ the macOS audio helper),
# and a pinned, checksum-verified `uv`. Output: desktop/engine/ and desktop/bin/ (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/../.."          # repository root
ROOT=$PWD
DESK=$ROOT/desktop
UV_VERSION=0.12.24                  # pinned; bump deliberately

echo "· web UI"
(cd ui && npm run build >/dev/null)

if [ "$(uname)" = "Darwin" ]; then
  echo "· macOS audio helper"
  bash modwen/native/macos/build.sh >/dev/null
fi

echo "· engine source"
rm -rf "$DESK/engine"
mkdir -p "$DESK/engine"
# Committed files only (never a stray local file), minus what the app doesn't need at runtime.
git ls-files --cached -- modwen pyproject.toml uv.lock README.md LICENSE \
  | grep -v -E '^modwen/native/macos/build/' \
  | while read -r f; do mkdir -p "$DESK/engine/$(dirname "$f")"; cp "$f" "$DESK/engine/$f"; done
mkdir -p "$DESK/engine/ui"
cp -R ui/dist "$DESK/engine/ui/dist"
if [ -x modwen/native/macos/build/modwen-tap ]; then
  mkdir -p "$DESK/engine/modwen/native/macos/build"
  cp modwen/native/macos/build/modwen-tap "$DESK/engine/modwen/native/macos/build/"
fi

echo "· uv $UV_VERSION"
EXE=uv
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) TRIPLE=aarch64-apple-darwin; ARCHIVE=uv-$TRIPLE.tar.gz ;;
  Darwin-x86_64) TRIPLE=x86_64-apple-darwin; ARCHIVE=uv-$TRIPLE.tar.gz ;;
  MINGW*-x86_64|MSYS*-x86_64|CYGWIN*-x86_64) TRIPLE=x86_64-pc-windows-msvc; ARCHIVE=uv-$TRIPLE.zip; EXE=uv.exe ;;
  *) echo "Unsupported platform for the desktop app: $(uname -s)-$(uname -m)"; exit 1 ;;
esac
CACHE=$ROOT/.cache/desktop-uv/$UV_VERSION
if [ ! -f "$CACHE/$EXE" ]; then
  mkdir -p "$CACHE"
  BASE=https://github.com/astral-sh/uv/releases/download/$UV_VERSION
  curl -fsSL "$BASE/$ARCHIVE" -o "$CACHE/$ARCHIVE"
  curl -fsSL "$BASE/$ARCHIVE.sha256" -o "$CACHE/$ARCHIVE.sha256"
  # refuse a download that doesn't match (shasum on macOS, sha256sum in Git Bash on Windows)
  (cd "$CACHE" && if command -v shasum >/dev/null; then shasum -a 256 -c "$ARCHIVE.sha256"; else sha256sum -c "$ARCHIVE.sha256"; fi)
  case "$ARCHIVE" in
    *.zip) unzip -q -o "$CACHE/$ARCHIVE" -d "$CACHE" ;;
    *) tar -xzf "$CACHE/$ARCHIVE" -C "$CACHE" --strip-components 1 ;;
  esac
fi
rm -rf "$DESK/bin"
mkdir -p "$DESK/bin"
cp "$CACHE/$EXE" "$DESK/bin/$EXE"

echo "Staged: $(du -sh "$DESK/engine" | cut -f1) engine, uv $("$DESK/bin/$EXE" --version | cut -d' ' -f2)"
