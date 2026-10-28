"""Write latest.json, the manifest the desktop app's updater reads from the newest GitHub release.

    python3 desktop/scripts/make-latest-json.py 0.5.0 notes.md release/desktop [--base URL] > latest.json

It lists every platform whose update file and .sig are in the folder:
  macOS (Apple Silicon): Modwen_<v>_aarch64.app.tar.gz (+ .sig), from desktop/scripts/build-macos.sh
  Windows (x64):         Modwen_<v>_x64-setup.exe (+ .sig), from the "Desktop app (Windows)" workflow
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

PLATFORMS = {  # updater platform key -> file name pattern
    "darwin-aarch64": "Modwen_{v}_aarch64.app.tar.gz",
    "darwin-x86_64": "Modwen_{v}_x64.app.tar.gz",
    "windows-x86_64": "Modwen_{v}_x64-setup.exe",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("notes", help="release notes (Markdown) shown in the app's 'What's new'")
    ap.add_argument("folder")
    ap.add_argument("--base", default="https://github.com/AbdullhRtima/modwen/releases/download/v{v}")
    a = ap.parse_args()
    folder, base = Path(a.folder), a.base.format(v=a.version)
    platforms = {}
    for key, pattern in PLATFORMS.items():
        f = folder / pattern.format(v=a.version)
        sig = f.with_name(f.name + ".sig")
        if f.exists() and sig.exists():
            platforms[key] = {"signature": sig.read_text().strip(), "url": f"{base}/{f.name}"}
    if not platforms:
        print(f"No update files for {a.version} in {folder}", file=sys.stderr)
        return 1
    print(json.dumps({
        "version": a.version,
        "notes": Path(a.notes).read_text(encoding="utf-8").strip(),
        "pub_date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }, ensure_ascii=False, indent=2))
    print("platforms: " + ", ".join(platforms), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
