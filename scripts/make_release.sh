#!/usr/bin/env bash
# Build a release zip that runs on macOS and Windows with a double-click (Modwen.command / Modwen.bat).
# Includes the prebuilt web UI and (when built on Apple Silicon) the macOS system-audio helper, so users need
# neither Node.js nor Xcode. Output: release/modwen-<version>.zip
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"(.*)"/\1/')
OUT="release/modwen-$VERSION"
echo "Building Modwen $VERSION"

(cd ui && npm ci --no-audit --no-fund && npm run build)
if [ "$(uname)" = "Darwin" ]; then bash modwen/native/macos/build.sh; fi

rm -rf "$OUT" "$OUT.zip"
mkdir -p "$OUT"
# Everything git would publish (respects .gitignore), plus the build outputs.
{ git ls-files --cached --others --exclude-standard; echo ui/dist; [ -f modwen/native/macos/build/modwen-tap ] && echo modwen/native/macos/build/modwen-tap; } \
  | grep -v -E '^(release/|\.github/)' | while read -r f; do
    [ -e "$f" ] || continue
    mkdir -p "$OUT/$(dirname "$f")"
    cp -R "$f" "$OUT/$f"
  done
chmod +x "$OUT/Modwen.command" "$OUT/scripts/"*.sh 2>/dev/null || true
(cd release && zip -qry "modwen-$VERSION.zip" "modwen-$VERSION")
echo "Wrote release/modwen-$VERSION.zip ($(du -h "release/modwen-$VERSION.zip" | cut -f1))"
