#!/bin/bash
# Modwen launcher for macOS. Double-click (first time: right-click → Open).
# First run sets everything up (a few minutes); later runs start in seconds. Close this window to quit Modwen.
cd "$(dirname "$0")" || exit 1
echo "Modwen · مُدوَّن"
echo

# Files downloaded from the internet are quarantined by macOS; clear it for this folder only.
xattr -dr com.apple.quarantine . 2>/dev/null || true

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (manages Modwen's private Python; nothing else on your system changes)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "Could not install uv. Check your internet connection."; read -r; exit 1; }
  export PATH="$HOME/.local/bin:$PATH"
fi

EXTRAS="--extra stt --extra desktop"
if [ "$(uname -m)" = "arm64" ]; then EXTRAS="$EXTRAS --extra mac"; fi

echo "Checking Modwen's environment..."
uv sync --frozen --no-dev $EXTRAS || { echo; echo "Setup failed (see above). Press Enter to close."; read -r; exit 1; }

if [ ! -f ui/dist/index.html ]; then
  if command -v npm >/dev/null 2>&1; then (cd ui && npm ci --no-audit --no-fund && npm run build); else
    echo "Note: the web UI is not built and Node.js is not installed. Use a release zip, or install Node.js."; fi
fi
if [ ! -x modwen/native/macos/build/modwen-tap ] && command -v swiftc >/dev/null 2>&1; then
  bash modwen/native/macos/build.sh || true
fi

echo
echo "Starting Modwen... your browser will open. Keep this window open; close it to quit."
exec uv run --frozen --no-dev modwen serve
