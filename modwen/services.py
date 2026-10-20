"""Wires config, storage, engines, recorder and processor together (one instance per app)."""

from __future__ import annotations

import itertools
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


def free_gpu_memory() -> None:
    """Drop MLX Whisper's own cached model and MLX's buffer cache (Apple Silicon), then collect garbage."""
    import gc
    import sys

    mod = sys.modules.get("mlx_whisper.transcribe")
    holder = getattr(mod, "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None
    gc.collect()
    try:
        import mlx.core as mx

        (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()
    except Exception:
        pass


class Services:
    def __init__(self, cfg: Config, *, stt_engine_factory=None, live_engine_factory=None, diarizer_factory=None,
                 llm_http_client=None, notifier: Notifier | None = None, source_factory=None, start_worker=True,
                 embedder_factory=None):
        self.cfg = cfg
        # Models load strictly from local files; only explicit download actions (setup / `modwen models
        # download`) go online.
        self.store = Store(cfg.db_path)
        self.store.sync_people()  # link speakers named before People links existed
        from modwen.audio.importer import remove_leftovers

        remove_leftovers(cfg.data_dir)  # encrypted temp files of an import that crashed
        self.keystore = KeyStore(cfg)
        self.bus = EventBus()
        self.notifier = notifier or Notifier(cfg, f"http://{cfg.app.ui_host}:{cfg.app.ui_port}")
        self._llm_http_client = llm_http_client
        self._stt_factory_override = stt_engine_factory
        self._live_factory_override = live_engine_factory
        self._diarizer_factory_override = diarizer_factory
        self._embedder_factory_override = embedder_factory
        self._engine_cache: dict[str, object] = {}
        self._engine_lock = threading.Lock()
        # Post-meeting work. New meetings first, then re-detect / regenerate; FIFO within a priority.
        self.jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._job_seq = itertools.count()
        self._pending: dict[str, int] = {}  # meeting id -> jobs queued or running for it
        self._pending_lock = threading.Lock()
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
            embedder_factory=self.embedder,
            release_stt=self._release_final_engine,
            recover_llm=self.recover_llm,
        )
        self.recorder = Recorder(cfg, self.store, self.keystore, self.bus.publish,
                                 on_stopped=self.enqueue_processing,
                                 live_engine_factory=self.live_engine if cfg.stt.live.provider != "none" else None,
                                 source_factory=source_factory)
        self._worker = threading.Thread(target=self._run_jobs, name="processing", daemon=True)
        if start_worker:
            self._worker.start()
            self._recover()
            threading.Thread(target=self._retention_loop, name="retention", daemon=True).start()

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

    def _release_final_engine(self) -> None:
        """After transcription, free the Whisper models on machines under 24 GB, so the local AI model has the
        (unified) GPU memory it needs: on a 16 GB Mac, Whisper + Qwen3-8B ran the GPU out of memory ("Compute
        error"). They reload in seconds for the next meeting."""
        ram = getattr(self.hardware, "ram_gb", None) or 0
        if ram and ram >= 24:
            return
        if self.recorder.session:  # live captions are in use
            return
        with self._engine_lock:
            for k in [k for k in self._engine_cache if k.startswith(("final:", "live:"))]:
                self._engine_cache.pop(k, None)
        free_gpu_memory()

    def recover_llm(self) -> bool:
        """After a GPU out-of-memory error the managed llama-server stays broken until restarted. Returns True
        when it was restarted (so the caller can retry once)."""
        if self.cfg.llm.active != "managed_qwen" or not self.llm_manager.status()["running"]:
            return False
        log.warning("The local AI model reported a compute error; restarting it")
        self._release_final_engine()
        try:
            self.llm_manager.stop()
            self.llm_manager.start(wait=180)
            return True
        except Exception as exc:
            log.warning("Could not restart the local AI model: %s", exc)
            if "stuck" in str(exc):  # only a computer restart helps: tell the user exactly that
                from modwen.notes.llm_client import LLMError

                raise LLMError(str(exc)) from exc
            return False

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

    def enroll_speaker(self, mid: str, sid: str, pid: str) -> dict:
        """Save a voiceprint of one speaker of a meeting for a person (consent recorded by the caller)."""
        from modwen.audio.chunks import read_channel
        from modwen.voiceprints import MIN_SECONDS, VoiceBank, enroll

        sp = next((x for x in self.store.get_speakers(mid) if x["id"] == sid), None)
        embedder = self.embedder()
        if sp is None or embedder is None:
            return {"saved": False, "seconds": 0.0, "reason": "Voice recognition is not available on this install"}
        channel = sp.get("channel") or "system"
        key = self.keystore.unwrap(self.store.get_wrapped_key(mid))
        try:
            audio = {channel: read_channel(self.cfg.audio_dir / mid, channel, key)}
        except Exception:
            return {"saved": False, "seconds": 0.0, "reason": "The recording of this meeting is no longer on disk"}
        saved, secs = enroll(VoiceBank(self.store, self.keystore), embedder, audio, self.store.get_segments(mid),
                             sp, pid, mid)
        if not saved:
            return {"saved": False, "seconds": round(secs, 1),
                    "reason": f"Only {secs:.0f} s of clean speech; at least {MIN_SECONDS:.0f} s is needed"}
        return {"saved": True, "seconds": round(secs, 1)}

    def embedder(self):
        """The voiceprint model (§5.1), or None when sherpa-onnx or its model is unavailable."""
        if self._embedder_factory_override:
            return self._embedder_factory_override()
        if self.cfg.diarization.provider == "none":
            return None

        def build():
            from modwen.diarize import SpeakerEmbedder

            return SpeakerEmbedder(self.cfg.models_dir)

        try:
            return self._engine("embedder", build)
        except Exception as exc:
            log.warning("Voiceprints unavailable: %s", exc)
            return None

    def llm(self, profile: str | None = None):
        from modwen.notes.llm_client import make_client

        return make_client(self.cfg, profile, http_client=self._llm_http_client)

    def notes_engine(self, profile: str | None = None):
        from modwen.notes.engine import NotesEngine

        return NotesEngine(self.llm(self.pick_llm(profile)), self.cfg.notes, self.cfg.notes.timezone)

    def pick_llm(self, preferred: str | None = None) -> str:
        """The AI model that will write notes: the meeting's own profile if it still exists, else the active one,
        else a fallback, whichever answers first. The managed model is started if it is installed but down."""
        from modwen.notes.llm_client import LLMError

        names: list[str] = []
        for n in (preferred, self.cfg.llm.active, *self.cfg.llm.fallback):
            if n and n in self.cfg.llm.profiles and n not in names:
                names.append(n)
        if not names:
            raise LLMError("No AI model is set up. Open Settings → AI model.")
        errors = []
        for n in names:
            if n == "managed_qwen":
                self._ensure_managed_running()
            health = self.llm_status(n)
            if health.get("ok"):
                if n != names[0]:
                    log.warning("AI model %s is not answering; writing notes with %s", names[0], n)
                return n
            errors.append(f"{n}: {health.get('error')}")
        raise LLMError("No AI model answered (" + "; ".join(errors) + "). Start your local model, then retry.")

    def _ensure_managed_running(self) -> None:
        st = self.llm_manager.status()
        if st["running"] or st["port_busy_external"] or not (st["binary_installed"] and st["model_installed"]):
            return
        try:
            log.info("Starting the managed AI model for notes")
            self.llm_manager.start(wait=180)
        except Exception as exc:
            log.warning("Managed AI model did not start: %s", exc)
            if "stuck" in str(exc):
                from modwen.notes.llm_client import LLMError

                raise LLMError(str(exc)) from exc

    def llm_status(self, profile: str | None = None) -> dict:
        try:
            return self.llm(profile).health()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----- jobs -----
    JOB_PRIORITY = {"process": 0, "redetect": 1, "regenerate": 2}

    def _enqueue(self, kind: str, meeting_id: str, kw: dict) -> None:
        with self._pending_lock:
            self._pending[meeting_id] = self._pending.get(meeting_id, 0) + 1
        self.jobs.put((self.JOB_PRIORITY.get(kind, 3), next(self._job_seq), kind, meeting_id, kw))

    def meetings_changed(self, mid: str, change: str) -> None:
        """Tell every open window that the meeting list changed (deleted, opened, pinned, imported...)."""
        self.bus.publish({"type": "meetings_changed", "meeting_id": mid, "change": change})

    def is_queued_or_running(self, meeting_id: str) -> bool:
        with self._pending_lock:
            return self._pending.get(meeting_id, 0) > 0

    def enqueue_processing(self, meeting_id: str) -> None:
        self._enqueue("process", meeting_id, {})

    def enqueue_regenerate(self, meeting_id: str, **kw) -> None:
        self._enqueue("regenerate", meeting_id, kw)

    def enqueue_redetect(self, meeting_id: str, **kw) -> None:
        # Marked as processing right away, so speaker edits are refused until the new speakers are saved.
        self.store.update_meeting(meeting_id, status="processing", error=None)
        self._enqueue("redetect", meeting_id, kw)

    def _run_jobs(self) -> None:
        while True:
            _, _, kind, mid, kw = self.jobs.get()
            try:
                if not self.store.get_meeting(mid):
                    log.info("Skipping %s for meeting %s: it was deleted", kind, mid)
                elif kind == "process":
                    self.processor.process(mid)
                elif kind == "regenerate":
                    self.processor.regenerate_notes(mid, **kw)
                elif kind == "redetect":
                    self.processor.redetect_speakers(mid, **kw)
            except Exception:
                if self.store.get_meeting(mid):
                    log.exception("job %s %s failed", kind, mid)
                else:  # deleted while it ran (e.g. by retention): nothing to save, not an error
                    log.info("Meeting %s was deleted while %s ran; its result was discarded", mid, kind)
            finally:
                with self._pending_lock:
                    left = self._pending.get(mid, 1) - 1
                    if left > 0:
                        self._pending[mid] = left
                    else:
                        self._pending.pop(mid, None)
                self.jobs.task_done()

    def apply_retention(self, now=None) -> dict:
        """Delete what is older than the retention settings (0 = keep forever). Pinned meetings are kept.

        Audio: the files go and the meeting's data key is dropped, so any copy left is unreadable. Transcript:
        the lines go (notes stay). Notes: the whole meeting goes."""
        import shutil
        from datetime import datetime, timedelta, timezone

        r = self.cfg.privacy.retention
        now = now or datetime.now(timezone.utc)
        done = {"audio": 0, "transcript": 0, "meeting": 0}
        for field, days in (("meeting", r.notes_days), ("transcript", r.transcript_days), ("audio", r.audio_days)):
            if not days or days <= 0:
                continue
            before = (now - timedelta(days=days)).isoformat(timespec="seconds")
            for m in self.store.retention_candidates(before, field):
                if self.is_queued_or_running(m["id"]):
                    continue
                if field in ("audio", "meeting"):
                    shutil.rmtree(self.cfg.audio_dir / m["id"], ignore_errors=True)
                if field == "meeting":
                    self.store.delete_meeting(m["id"])
                    self.meetings_changed(m["id"], "deleted")
                elif field == "transcript":
                    self.store.forget_transcript(m["id"])
                else:
                    self.store.forget_audio(m["id"])
                done[field] += 1
        if any(done.values()):
            log.info("Retention clean-up: %s", done)
        return done

    def _retention_loop(self) -> None:
        import time

        time.sleep(30)  # let startup finish first
        while True:
            try:
                self.apply_retention()
            except Exception:
                log.exception("Retention clean-up failed")
            time.sleep(6 * 3600)

    def _recover(self) -> None:
        """Meetings left 'recording'/'processing' by a crash are re-processed from their saved chunks."""
        for m in self.store.list_meetings(limit=50):
            if m["status"] in ("recording", "processing"):
                if m["status"] == "recording" and not any((self.cfg.audio_dir / m["id"]).glob("*.flac.enc")):
                    # closed before the first chunk was written: there is nothing to process, say so plainly
                    log.warning("Meeting %s ended before any audio was saved", m["id"])
                    self.store.update_meeting(m["id"], status="failed", error=(
                        f"Modwen closed before any audio was saved (audio is saved every "
                        f"{self.cfg.capture.chunk_seconds:g} s). Nothing to process."))
                    continue
                log.warning("Recovering meeting %s left in state %s", m["id"], m["status"])
                self.store.update_meeting(m["id"], status="processing")
                self.enqueue_processing(m["id"])
