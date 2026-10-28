#!/usr/bin/env bash
# Build the macOS desktop release: Modwen.app signed with Modwen's stable self-signed identity, the .dmg, and the
# signed update archive (.app.tar.gz + .sig) for in-app updates.
#
# Needs (git-ignored, never committed), from .cache/desktop-signing/ or the environment (CI secrets):
#   codesign.p12 + codesign.p12.password        -> MODWEN_CODESIGN_P12 (base64) + MODWEN_CODESIGN_P12_PASSWORD
#   modwen-updater.key + modwen-updater.password -> TAURI_SIGNING_PRIVATE_KEY + TAURI_SIGNING_PRIVATE_KEY_PASSWORD
# Why self-signed: macOS ties microphone / system-audio permission and Keychain "Always Allow" to the signature,
# so a stable identity keeps them across updates (ad-hoc signatures change on every build).
set -euo pipefail
cd "$(dirname "$0")/.."              # desktop/
ROOT=$(cd .. && pwd)
KEYS=$ROOT/.cache/desktop-signing
IDENTITY="Modwen Self-Signed"
export RUSTUP_HOME=${RUSTUP_HOME:-$ROOT/.cache/rustup} CARGO_HOME=${CARGO_HOME:-$ROOT/.cache/cargo}
export PATH=$CARGO_HOME/bin:$PATH npm_config_cache=${npm_config_cache:-$ROOT/.cache/npm}

P12_B64=${MODWEN_CODESIGN_P12:-$( [ -f "$KEYS/codesign.p12" ] && base64 -i "$KEYS/codesign.p12" || true)}
P12_PW=${MODWEN_CODESIGN_P12_PASSWORD:-$(cat "$KEYS/codesign.p12.password" 2>/dev/null || true)}
export TAURI_SIGNING_PRIVATE_KEY=${TAURI_SIGNING_PRIVATE_KEY:-$(cat "$KEYS/modwen-updater.key" 2>/dev/null || true)}
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-$(cat "$KEYS/modwen-updater.password" 2>/dev/null || true)}
[ -n "$P12_B64" ] && [ -n "$P12_PW" ] || { echo "Missing the code-signing certificate (see the header)"; exit 1; }
[ -n "$TAURI_SIGNING_PRIVATE_KEY" ] || { echo "Missing the updater key (see the header)"; exit 1; }

VERSION=$(python3 -c "import json;print(json.load(open('src-tauri/tauri.conf.json'))['version'])")
ARCH=$(uname -m); [ "$ARCH" = arm64 ] && ARCH=aarch64
OUT=$ROOT/release/desktop
mkdir -p "$OUT"

echo "== stage + build (unsigned)"
bash scripts/stage-engine.sh
# MODWEN_TEST_CONFIG: extra Tauri config for local test builds only (e.g. an http update server); never for releases
npx tauri build --bundles app --no-sign ${MODWEN_TEST_CONFIG:+--config "$MODWEN_TEST_CONFIG"}
APP=src-tauri/target/release/bundle/macos/Modwen.app

echo "== sign with a temporary keychain (deleted at the end; the login keychain isn't touched)"
TMP=$(mktemp -d)
KC=$TMP/modwen-build.keychain-db
KCPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
OLD_KEYCHAINS=$(security list-keychains -d user | tr -d '"')
cleanup() { security list-keychains -d user -s $OLD_KEYCHAINS >/dev/null 2>&1 || true; security delete-keychain "$KC" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
security create-keychain -p "$KCPW" "$KC"
security set-keychain-settings -lut 1800 "$KC"
security unlock-keychain -p "$KCPW" "$KC"
printf "%s" "$P12_B64" | base64 -d > "$TMP/cert.p12"
security import "$TMP/cert.p12" -k "$KC" -P "$P12_PW" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPW" "$KC" >/dev/null
security list-keychains -d user -s "$KC" $OLD_KEYCHAINS
sign() { codesign --force --timestamp=none --keychain "$KC" --sign "$IDENTITY" "$@"; }
sign "$APP/Contents/Resources/bin/uv"
[ -f "$APP/Contents/Resources/engine/modwen/native/macos/build/modwen-tap" ] && sign "$APP/Contents/Resources/engine/modwen/native/macos/build/modwen-tap"
sign "$APP"
codesign --verify --strict "$APP"
echo "designated requirement: $(codesign -d -r- "$APP" 2>&1 | grep designated | cut -c1-120)…"

echo "== update archive (signed with the updater key) + dmg"
TGZ=$OUT/Modwen_${VERSION}_${ARCH}.app.tar.gz
# no AppleDouble "._*" metadata entries: the updater can't unpack them (every update failed with them)
COPYFILE_DISABLE=1 tar --no-mac-metadata -czf "$TGZ" -C "$(dirname "$APP")" Modwen.app
tar -tzf "$TGZ" | grep -q '/\._\|^\._' && { echo "update archive has ._ entries"; exit 1; }
npx tauri signer sign "$TGZ" >/dev/null
DMG=$OUT/Modwen_${VERSION}_${ARCH}.dmg
rm -f "$DMG"
mkdir -p "$TMP/dmg" && cp -R "$APP" "$TMP/dmg/" && ln -s /Applications "$TMP/dmg/Applications"
hdiutil create -quiet -volname "Modwen $VERSION" -srcfolder "$TMP/dmg" -ov -format UDZO "$DMG"
sign "$DMG"
ls -la "$OUT" | grep -E "Modwen_${VERSION}"
