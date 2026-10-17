"""Resumable HTTPS downloads with progress, and archive extraction.

Only used for explicit, user-started downloads of software and models (llama.cpp, Qwen GGUF, Whisper).
Meeting audio and transcripts are never sent anywhere.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

import httpx

Progress = Callable[[int, int], None]


def download(url: str, dest: Path, progress: Progress | None = None, chunk: int = 1 << 20) -> Path:
    """Download ``url`` to ``dest`` (resuming a previous ``.part`` file). Returns ``dest``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:  # already complete
                part.replace(dest)
                return dest
            r.raise_for_status()
            if r.status_code == 200 and have:  # server ignored Range: start over
                have = 0
            total = int(r.headers.get("content-length", 0)) + have
            mode = "ab" if have else "wb"
            done = have
            with open(part, mode) as fh:
                for block in r.iter_bytes(chunk):
                    fh.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
    part.replace(dest)
    return dest


def extract(archive: Path, target: Path) -> Path:
    """Extract a .zip or .tar.gz safely into ``target`` (no paths escaping it)."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                out = (target / m.filename).resolve()
                if not str(out).startswith(str(root)):
                    raise ValueError(f"unsafe path in archive: {m.filename}")
            z.extractall(target)
    else:
        with tarfile.open(archive, "r:*") as t:
            for m in t.getmembers():
                out = (target / m.name).resolve()
                if not str(out).startswith(str(root)) or m.issym() and os.path.isabs(m.linkname):
                    raise ValueError(f"unsafe path in archive: {m.name}")
            t.extractall(target)
    return target


def dir_size(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total
