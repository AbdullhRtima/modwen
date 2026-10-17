"""Background jobs (downloads, installs) with progress that the UI can follow over the event bus."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    kind: str
    title: str
    status: str = "running"  # running | done | failed
    done_bytes: int = 0
    total_bytes: int = 0
    step: str = ""
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    @property
    def fraction(self) -> float:
        return round(self.done_bytes / self.total_bytes, 4) if self.total_bytes else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fraction"] = self.fraction
        return d


class JobManager:
    def __init__(self, publish: Callable[[dict], None] | None = None):
        self.publish = publish or (lambda e: None)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._last_emit: dict[str, float] = {}

    def running(self, kind: str) -> Job | None:
        with self._lock:
            return next((j for j in self.jobs.values() if j.kind == kind and j.status == "running"), None)

    def start(self, kind: str, title: str, fn: Callable[[Job, Callable[..., None]], None]) -> Job:
        """Run ``fn(job, update)`` in a thread. Only one running job per kind."""
        existing = self.running(kind)
        if existing:
            return existing
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title)
        with self._lock:
            self.jobs[job.id] = job

        def update(done: int | None = None, total: int | None = None, step: str | None = None, force=False):
            if done is not None:
                job.done_bytes = done
            if total is not None:
                job.total_bytes = total
            if step is not None:
                job.step = step
                force = True
            now = time.time()
            if force or now - self._last_emit.get(job.id, 0) > 0.5:
                self._last_emit[job.id] = now
                self.publish({"type": "job", "job": job.to_dict()})

        def run():
            try:
                fn(job, update)
                job.status = "done"
            except Exception as exc:
                log.error("job %s failed: %s\n%s", kind, exc, traceback.format_exc())
                job.status, job.error = "failed", str(exc)
            job.finished = time.time()
            update(force=True)

        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        update(force=True)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)]
