# Releasing Modwen

How a release is built and published. Everything runs on a Mac (Apple Silicon); CI on GitHub only runs the
tests, it doesn't build or publish.

> **Rule:** nothing is committed, pushed or released until it has been tested locally on real meetings.

## 0. One-time setup

| Tool | Why | Check |
|---|---|---|
| `uv` 0.9+ | the lockfile format needs a recent uv (an old one rewrites `uv.lock` and drops `sherpa-onnx-core`) | `uv --version` (or use `uvx --from 'uv>=0.9' uv …` as below) |
| Node.js 20 + npm | builds the web UI | `node --version` |
| Xcode Command Line Tools | compiles the macOS system-audio helper (`swiftc`) | `swiftc --version` |
| GitHub CLI, logged in | tags, pushes and publishes the release | `gh auth status` |

Keep caches inside the repository (see `CLAUDE.md`):

```bash
cd ~/code/modwen
export UV_CACHE_DIR=$PWD/.cache/uv UV_TOOL_DIR=$PWD/.cache/uv-tools
```

## 1. Check everything is green

```bash
git status                                   # clean, on main
uv run pytest -q                             # all tests pass (no network, no models)
uv run ruff check --select E9,F modwen tests # no errors
(cd ui && npm run build)                     # UI type-checks and builds
gh run list --limit 3                        # the last CI run on main is green (macOS + Windows)
```

## 2. Bump the version

The version lives in five places (the desktop app's `tauri.conf.json`, `Cargo.toml` and `package.json` too).
Replace `0.3.0` with the new version:

```bash
NEW=0.3.1
sed -i '' "s/^version = \".*\"/version = \"$NEW\"/" pyproject.toml
sed -i '' "s/__version__ = \".*\"/__version__ = \"$NEW\"/" modwen/__init__.py
sed -i '' "s/\"version\": \"[0-9.]*\"/\"version\": \"$NEW\"/" ui/package.json desktop/package.json desktop/src-tauri/tauri.conf.json
sed -i '' "s/^version = \".*\"/version = \"$NEW\"/" desktop/src-tauri/Cargo.toml
python3 - "$NEW" <<'EOF'
import json, pathlib, sys
p = pathlib.Path("ui/package-lock.json"); d = json.loads(p.read_text())
d["version"] = d["packages"][""]["version"] = sys.argv[1]
p.write_text(json.dumps(d, indent=2) + "\n")
EOF
uvx --from 'uv>=0.9' uv lock                 # updates modwen's own version in uv.lock
git diff --stat                              # 5 files, one line each
```

Run the tests once more, then commit:

```bash
uv run pytest -q
git add -A
git commit -m "Modwen $NEW"
```

## 3. Build the release zip

```bash
bash scripts/make_release.sh
```

This builds the web UI and the macOS helper (`modwen/native/macos/build/modwen-tap`, Apple Silicon), then packs
everything git tracks plus those two build outputs into `release/modwen-$NEW.zip`. The `release/` folder is
git-ignored.

Check what's inside:

```bash
Z=release/modwen-$NEW.zip
# Must be 0: no recordings, data, config, planning notes or environments
unzip -l $Z | grep -c -E "\.tmp/|\.data/|/config\.yaml$|\.wav$|\.flac|docs/planning|\.venv/|\.cache/"
# Must list all of these
unzip -l $Z | grep -E "ui/dist/index.html|build/modwen-tap$|Modwen.command|Modwen.bat|uv.lock"
```

## 4. Smoke-test the zip itself

Unpack it somewhere fresh and run it the way a user would:

```bash
T=$(mktemp -d)
unzip -q $Z -d $T && cd $T/modwen-$NEW
uv sync --frozen --extra stt --extra desktop --extra mac
uv run --frozen python -c "import modwen, sherpa_onnx; print(modwen.__version__)"
uv run --frozen pytest -q
cd - && rm -rf $T
```

For a real check, also start it (`uv run --frozen modwen serve` from the unpacked folder) and record a short
meeting.

## 5. Tag and push

```bash
git tag -a v$NEW -m "Modwen $NEW"
git push origin main
git push origin v$NEW
```

The push to `main` starts CI; wait for it to be green on both platforms:

```bash
gh run watch "$(gh run list --workflow CI --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
```

## 6. Publish the GitHub release

Write the release notes (for users: what's new, how to upgrade, known limits) in a file, then:

```bash
# Stable: shows as "Latest" on the repo page
gh release create v$NEW release/modwen-$NEW.zip --title "Modwen $NEW" --notes-file notes.md

# Or a beta: marked "Pre-release". GitHub never shows a pre-release as "Latest".
gh release create v$NEW release/modwen-$NEW.zip --prerelease --title "Modwen $NEW (beta)" --notes-file notes.md
```

Check it:

```bash
gh release list --limit 3
```

### Changing a release after publishing

```bash
# Promote a beta to the stable "Latest" release (same zip)
gh release edit v$NEW --prerelease=false --latest --title "Modwen $NEW"

# Replace the notes
gh release edit v$NEW --notes-file notes.md

# Replace the zip (e.g. a rebuilt one) on the same release
gh release upload v$NEW release/modwen-$NEW.zip --clobber
```

Don't move a pushed tag. If the code changes, release a new version (`0.3.1`) instead.

## 6b. The desktop app (macOS + Windows)

The desktop app (`desktop/`, Tauri) bundles `uv`, the engine's source and the built UI; on first start it sets
up Modwen's private Python in the data folder. Design of updates: `docs/planning/04-desktop-updates.md`.

**Keys** (git-ignored, in `.cache/desktop-signing/`; back them up offline, never commit them):

| File | What | Also stored as |
|---|---|---|
| `modwen-updater.key` + `.password` | signs update files; the app only installs updates signed with it | GitHub secrets `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` |
| `modwen-updater.key.pub` | the matching public key, built into the app (`tauri.conf.json`) | |
| `codesign.p12` + `.password` | the stable "Modwen Self-Signed" macOS identity (keeps microphone permission and Keychain "Always Allow" across updates) | |

Losing the updater key means desktop users have to reinstall by hand once. Rotate it by shipping a version with
the new public key while the old key still signs it.

**macOS** (on the Mac): builds, signs (temporary keychain, deleted afterwards), makes the `.dmg` and the signed
update archive in `release/desktop/`:

```bash
bash desktop/scripts/build-macos.sh
```

**Windows** (GitHub Actions, "Desktop app (Windows)"): runs on the version tag push, or by hand
(`gh workflow run desktop.yml`). It builds `Modwen_<v>_x64-setup.exe` + `.sig`, installs it silently on the runner
and runs the app's self-test; the run fails if any check is missing. Download the installer when it's green:

```bash
gh run download "$(gh run list --workflow desktop.yml --limit 1 --json databaseId --jq '.[0].databaseId')" \
  --name modwen-windows --dir release/desktop
```

**Update manifest** (after both builds): what the installed apps read to find the new version.

```bash
python3 desktop/scripts/make-latest-json.py $NEW notes.md release/desktop > release/desktop/latest.json
```

**Publish** with the zip, both installers, both update files and `latest.json` (the updater reads
`releases/latest/download/latest.json`, so the release must be the Latest one). The website's download buttons
use fixed names (`releases/latest/download/Modwen-macOS.dmg`), so add those copies too:

```bash
cp release/desktop/Modwen_${NEW}_aarch64.dmg release/desktop/Modwen-macOS.dmg
cp release/desktop/Modwen_${NEW}_x64-setup.exe release/desktop/Modwen-Windows-setup.exe
gh release create v$NEW release/modwen-$NEW.zip release/desktop/Modwen_${NEW}_aarch64.dmg \
  release/desktop/Modwen_${NEW}_aarch64.app.tar.gz release/desktop/Modwen_${NEW}_aarch64.app.tar.gz.sig \
  release/desktop/Modwen_${NEW}_x64-setup.exe release/desktop/Modwen_${NEW}_x64-setup.exe.sig \
  release/desktop/latest.json release/desktop/Modwen-macOS.dmg release/desktop/Modwen-Windows-setup.exe \
  --latest --title "Modwen $NEW" --notes-file notes.md
```

**Test an update before publishing it** (optional): build the current version with
`MODWEN_TEST_CONFIG='{"plugins":{"updater":{"dangerousInsecureTransportProtocol":true}}}'`, serve a newer
build's `latest.json` from `http://127.0.0.1:<port>`, and run the app with `MODWEN_UPDATE_ENDPOINT` set and
`MODWEN_APP_SELFTEST=update` (results in `logs/app-engine.log`). Never ship a build made with `MODWEN_TEST_CONFIG`.

## 7. The website

The landing page is `docs/index.html`, served by GitHub Pages from `main` at https://modwen.rtima.org/.
Pushing `main` publishes it; the "pages build and deployment" run in `gh run list` shows when it's live.

## Commits

- Every commit message ends with the co-author line used in this repo.
- One topic per commit where the files allow it: a fix, the version bump, and a website change are separate
  commits.
- Pull requests end with the "Generated with Claude Code" line.
