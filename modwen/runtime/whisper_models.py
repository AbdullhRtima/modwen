"""Whisper model presence checks and explicit downloads (with progress) into Modwen's models dir."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from modwen.runtime.download import dir_size
from modwen.stt.engines import MLX_REPOS


def repo_for(model: str, backend: str) -> str:
    if backend == "mlx":
        return MLX_REPOS.get(model, model)
    from faster_whisper.utils import _MODELS

    return _MODELS.get(model, model)


def cache_dir(cfg, backend: str) -> Path:
    return cfg.models_dir / ("mlx" if backend == "mlx" else "whisper")


def repo_dir(cfg, model: str, backend: str) -> Path:
    return cache_dir(cfg, backend) / ("models--" + repo_for(model, backend).replace("/", "--"))


def is_present(cfg, model: str, backend: str) -> bool:
    """True if the model snapshot exists locally (no network)."""
    snaps = repo_dir(cfg, model, backend) / "snapshots"
    return snaps.is_dir() and any(any(s.iterdir()) for s in snaps.iterdir() if s.is_dir())


def download(cfg, model: str, backend: str, update=None) -> None:
    """Download one Whisper model, reporting byte progress via ``update(done=, total=)``."""
    from huggingface_hub import HfApi, snapshot_download

    repo = repo_for(model, backend)
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        total = sum((s.size or 0) for s in info.siblings or [])
    except Exception:
        total = 0
    target = repo_dir(cfg, model, backend)
    done = threading.Event()

    def watch():
        while not done.is_set():
            if update:
                update(done=dir_size(target), total=total)
            time.sleep(1)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    try:
        snapshot_download(repo, cache_dir=str(cache_dir(cfg, backend)), local_files_only=False)
    finally:
        done.set()
    if update:
        update(done=total or dir_size(target), total=total or dir_size(target))
