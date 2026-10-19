"""Wires config, storage, engines, recorder and processor together (one instance per app)."""

from __future__ import annotations

import logging
import queue
import threading
from functools import cached_property

from modwen.config import Config
from modwen.crypto import KeyStore
from modwen.events import EventBus
from modwen.hardware import TierPlan, choose_tier, detect
from modwen.notify import Notifier
from modwen.storage import Store

log = logging.getLogger(__name__)


class Services:
    def __init__(self, cfg: Config, *, stt_engine_factory=None, live_engine_factory=None, diarizer_factory=None,
                 llm_http_client=None, notifier: Notifier | None = None, source_factory=None, start_worker=True):
        self.cfg = cfg
        # Models load strictly from local files; only explicit download actions (setup / `modwen models
        # download`) go online.
        self.store = Store(cfg.db_path)
        self.keystore = KeyStore(cfg)
        self.bus = EventBus()
        self.notifier = notifier or Notifier(cfg, f"http://{cfg.app.ui_host}:{cfg.app.ui_port}")
        self._llm_http_client = llm_http_client
        self._stt_factory_override = stt_engine_factory
        self._live_factory_override = live_engine_factory
        self._diarizer_factory_override = diarizer_factory
        self._engine_cache: dict[str, object] = {}
        self._engine_lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()  # post-meeting processing queue
        from modwen.runtime.jobs import JobManager
        from modwen.runtime.llm_manager import ManagedLLM

        self.tasks = JobManager(self.bus.publish)  # downloads/installs with progress
        self.llm_manager = ManagedLLM(cfg)

        from modwen.pipeline.processor import Processor
        from modwen.pipeline.recorder import Recorder

        self.processor = Processor(
            cfg, self.store, self.keystore,
            stt_engine_factory=self.final_engine,
            diarizer_factory=self.diarizer,
            notes_engine_factory=self.notes_engine,
            notifier=self.notifier,
            publish=self.bus.publish,
        )
        self.recorder = Recorder(cfg, self.store, self.keystore, self.bus.publish,
                                 on_stopped=self.enqueue_processing,
                                 live_engine_factory=self.live_engine if cfg.stt.live.provider != "none" else None,
                                 source_factory=source_factory)
        self._worker = threading.Thread(target=self._run_jobs, name="processing", daemon=True)
        if start_worker:
            self._worker.start()
            self._recover()

    # ----- hardware / models -----
    @cached_property
    def hardware(self):
        return detect(self.cfg.data_dir)

    @cached_property
    def plan(self) -> TierPlan:
        return choose_tier(self.hardware)

    @cached_property
    def model_choice(self) -> tuple[str, str, str]:
        from modwen.stt.engines import resolve_models

        return resolve_models(self.cfg, self.plan)

    def reset_engines(self) -> None:
        """Forget loaded Whisper engines and the cached model choice (after settings/model changes)."""
        with self._engine_lock:
            self._engine_cache.clear()
        self.__dict__.pop("model_choice", None)

    def start_managed_llm(self) -> None:
        """Start the managed AI model in the background if it's installed and set to autostart."""
        m = self.cfg.llm.managed
        if not (m.enabled and m.autostart and self.cfg.llm.active == "managed_qwen"):
            return
        st = self.llm_manager.status()
        if not (st["binary_installed"] and st["model_installed"]) or st["port_busy_external"]:
            return

        def run():
            try:
                self.llm_manager.start()
            except Exception as exc:
                log.warning("Managed AI model did not start: %s", exc)

        threading.Thread(target=run, name="managed-llm", daemon=True).start()

    def _engine(self, key: str, build):
        with self._engine_lock:
            if key not in self._engine_cache:
                self._engine_cache[key] = build()
            return self._engine_cache[key]

    def final_engine(self):
        if self._stt_factory_override:
            return self._stt_factory_override()
        from modwen.stt.engines import build_engine

        backend, _, final = self.model_choice
        return self._engine(f"final:{backend}:{final}",
                            lambda: build_engine(self.cfg, final, backend, self.cfg.stt.final))

    def live_engine(self):
        if self._live_factory_override:
            return self._live_factory_override()
        from modwen.stt.engines import build_engine

        backend, live, _ = self.model_choice
        return self._engine(f"live:{backend}:{live}",
                            lambda: build_engine(self.cfg, live, backend, self.cfg.stt.live))

    def diarizer(self):
        if self._diarizer_factory_override:
            return self._diarizer_factory_override()
        from modwen.diarize import make_diarizer

        d = self._engine("diarizer", lambda: make_diarizer(self.cfg))
        if d.name == "single" and self.cfg.diarization.provider != "none":
            # A fallback (e.g. the model download failed while offline): try again for the next meeting.
            with self._engine_lock:
                self._engine_cache.pop("diarizer", None)
        return d

    def llm(self, profile: str | None = None):
        from modwen.notes.llm_client import make_client

        return make_client(self.cfg, profile, http_client=self._llm_http_client)

    def notes_engine(self, profile: str | None = None):
        from modwen.notes.engine import NotesEngine

        return NotesEngine(self.llm(profile or self.cfg.llm.active), self.cfg.notes, self.cfg.notes.timezone)

    def llm_status(self, profile: str | None = None) -> dict:
        try:
            return self.llm(profile).health()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----- jobs -----
    def enqueue_processing(self, meeting_id: str) -> None:
        self.jobs.put(("process", meeting_id, {}))

    def enqueue_regenerate(self, meeting_id: str, **kw) -> None:
        self.jobs.put(("regenerate", meeting_id, kw))

    def enqueue_redetect(self, meeting_id: str, **kw) -> None:
        self.jobs.put(("redetect", meeting_id, kw))

    def _run_jobs(self) -> None:
        while True:
            kind, mid, kw = self.jobs.get()
            try:
                if kind == "process":
                    self.processor.process(mid)
                elif kind == "regenerate":
                    self.processor.regenerate_notes(mid, **kw)
                elif kind == "redetect":
                    self.processor.redetect_speakers(mid, **kw)
            except Exception:
                log.exception("job %s %s failed", kind, mid)
            finally:
                self.jobs.task_done()

    def _recover(self) -> None:
        """Meetings left 'recording'/'processing' by a crash are re-processed from their saved chunks."""
        for m in self.store.list_meetings(limit=50):
            if m["status"] in ("recording", "processing"):
                log.warning("Recovering meeting %s left in state %s", m["id"], m["status"])
                self.store.update_meeting(m["id"], status="processing")
                self.enqueue_processing(m["id"])
