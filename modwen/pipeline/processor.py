"""Post-meeting processing: final STT -> diarization -> merge -> names -> notes -> notification."""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from modwen.audio.chunks import list_channels, read_channel
from modwen.pipeline.speakers import carry_names, label_speakers
from modwen.storage import now_iso
from modwen.stt.transcriber import transcribe_channel

log = logging.getLogger(__name__)

# Share of the total time per step, measured on a 2-hour meeting (M1 16 GB: 34 / 10 / 46 min). Only a first guess:
# once notes parts are written, the estimate uses their real pace.
STEP_WEIGHTS = {"transcribe": 0.38, "speakers": 0.11, "notes": 0.51}


@dataclass
class Progress:
    meeting_id: str
    publish: Callable[[dict], None] | None = None
    state: dict = field(default_factory=lambda: {
        "step": "transcribe", "steps": {"audio": "done", "transcribe": "running", "speakers": "pending",
                                       "notes": "pending"},
        "transcribe": 0.0, "notes_part": 0, "notes_parts": 0, "started": time.time(), "eta_seconds": None,
    })

    def update(self, store, **changes) -> None:
        if changes.get("step") and changes["step"] != self.state.get("step"):
            self.state["step_started"] = time.time()
        self.state.update(changes)
        done = (STEP_WEIGHTS["transcribe"] * self.state.get("transcribe", 0)
                + (STEP_WEIGHTS["speakers"] if self.state["steps"]["speakers"] == "done" else 0)
                + STEP_WEIGHTS["notes"] * (self.state["notes_part"] / max(1, self.state["notes_parts"])
                                           if self.state["notes_parts"] else 0))
        elapsed = time.time() - self.state["started"]
        self.state["overall"] = round(done, 3)
        self.state["eta_seconds"] = int(elapsed / done - elapsed) if done > 0.05 else None
        parts, written = self.state["notes_parts"], self.state["notes_part"]
        if self.state.get("step") == "notes" and parts and 0 < written < parts:
            # the notes' own pace (the AI model's speed varies far more between computers than Whisper's)
            per_part = (time.time() - self.state.get("step_started", self.state["started"])) / written
            self.state["eta_seconds"] = int(per_part * (parts - written))
        store.update_meeting(self.meeting_id, progress=self.state)
        if self.publish:
            self.publish({"type": "progress", "meeting_id": self.meeting_id, "progress": self.state})


class Processor:
    """Runs the pipeline for one meeting. Engines are injected so tests can use fakes."""

    def __init__(self, cfg, store, keystore, *, stt_engine_factory, diarizer_factory, notes_engine_factory,
                 notifier=None, publish: Callable[[dict], None] | None = None, embedder_factory=None,
                 release_stt: Callable[[], None] | None = None, recover_llm: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.store = store
        self.keystore = keystore
        self.stt_engine_factory = stt_engine_factory
        self.diarizer_factory = diarizer_factory
        self.notes_engine_factory = notes_engine_factory
        self.notifier = notifier
        self.publish = publish
        self.embedder_factory = embedder_factory  # voiceprints (§5.1); None = feature unavailable
        self.release_stt = release_stt  # frees the Whisper model before the notes step on smaller machines
        self.recover_llm = recover_llm  # restarts a local AI model left broken by a GPU out-of-memory error

    # ------------------------------------------------------------------
    def process(self, meeting_id: str, *, skip_transcription: bool = False, redetect: bool = False,
                num_speakers: int | None = None) -> dict | None:
        store = self.store
        m = store.get_meeting(meeting_id)
        if not m:
            raise KeyError(meeting_id)
        prog = Progress(meeting_id, self.publish)
        store.update_meeting(meeting_id, status="processing", error=None)
        self._emit({"type": "status", "meeting_id": meeting_id, "status": "processing"})
        try:
            if redetect:
                self._redetect(m, prog, num_speakers)
            elif not skip_transcription:
                self._transcribe_and_merge(m, prog)
            prog.state["steps"].update({"transcribe": "done", "speakers": "done", "notes": "running"})
            prog.update(store, step="notes", transcribe=1.0)
            if self.release_stt:
                self.release_stt()  # also when transcription was skipped (regenerate / re-detect)
            notes = self._notes_with_recovery(m, prog)
            prog.state["steps"]["notes"] = "done"
            prog.update(store, step="done", notes_part=prog.state["notes_parts"] or 1,
                        notes_parts=prog.state["notes_parts"] or 1)
            store.update_meeting(meeting_id, status="ready", notified_at=now_iso())
            self._emit({"type": "notes_ready", "meeting_id": meeting_id, "title": m["title"]})
            self._notify_ready(m, notes)
            return notes
        except Exception as exc:
            log.error("Processing failed for %s: %s\n%s", meeting_id, exc, traceback.format_exc())
            step = prog.state.get("step", "notes")
            prog.state["steps"][step if step in prog.state["steps"] else "notes"] = "failed"
            store.update_meeting(meeting_id, status="failed", error=f"{step}: {exc}", progress=prog.state)
            self._emit({"type": "failed", "meeting_id": meeting_id, "error": str(exc), "step": step})
            if self.notifier and self.cfg.notify.on_failure:
                kept = "The transcript is saved." if step == "notes" else "The recording is saved."
                self.notifier.notify(f"Notes failed: {m['title']}", f"{kept} Open Modwen to retry.", meeting_id)
            return None

    def regenerate_notes(self, meeting_id: str, summary_language: str | None = None,
                         llm_profile: str | None = None, scope: str = "all") -> dict | None:
        if summary_language:
            self.store.update_meeting(meeting_id, summary_language=summary_language)
        if llm_profile:
            self.store.update_meeting(meeting_id, llm_profile=llm_profile)
        if scope == "summary" and self.store.latest_notes(meeting_id):
            return self.regenerate_summary(meeting_id)
        return self.process(meeting_id, skip_transcription=True)

    def redetect_speakers(self, meeting_id: str, num_speakers: int | None = None) -> dict | None:
        """Run speaker detection again on the saved transcript (text edits kept), then rewrite the notes."""
        return self.process(meeting_id, redetect=True, num_speakers=num_speakers)

    def regenerate_summary(self, meeting_id: str) -> dict | None:
        """Rewrite only the summary from the current (possibly hand-edited) items. Edits are kept."""
        from modwen.notes.dates import meeting_date
        from modwen.notes.engine import refresh_warnings

        m = self.store.get_meeting(meeting_id)
        notes = self.store.latest_notes(meeting_id)
        if not m or not notes:
            return None
        data = dict(notes["data"])
        lang = m.get("summary_language") or self.cfg.notes.summary_language
        langs = ["en", "ar"] if lang == "both" else [lang]
        engine = self.notes_engine_factory(m.get("llm_profile"))
        base = meeting_date(m.get("started_at"), self.cfg.notes.timezone)
        try:
            summary = dict(data.get("summary", {}))
            for code in langs:
                summary[code] = engine._summary(m["title"], base, data, code)
        except Exception as exc:
            log.error("Summary regeneration failed for %s: %s", meeting_id, exc)
            self._emit({"type": "failed", "meeting_id": meeting_id, "error": str(exc), "step": "summary"})
            return None
        data["summary"] = summary
        data["warnings"] = refresh_warnings(data)
        saved = self.store.save_notes(meeting_id, data, llm_profile=notes["llm_profile"], model=engine.llm.profile.model,
                                      prompt_version=notes["prompt_version"])
        self._emit({"type": "notes_ready", "meeting_id": meeting_id, "title": m["title"], "summary_only": True})
        return saved

    # ------------------------------------------------------------------
    def _emit(self, event: dict) -> None:
        if self.publish:
            self.publish(event)

    def _transcribe_and_merge(self, m: dict, prog: Progress) -> None:
        cfg, store = self.cfg, self.store
        mid = m["id"]
        audio = self._load_audio(m)
        total = sum(a.size for a in audio.values()) or 1
        duration_ms = int(max(a.size for a in audio.values()) / 16000 * 1000)
        store.update_meeting(mid, duration_ms=duration_ms)

        engine = self.stt_engine_factory()
        mode = m.get("meeting_language") or cfg.stt.meeting_language
        per_channel: dict[str, list[dict]] = {}
        done_samples = 0
        for ch, a in audio.items():
            base = done_samples

            def on_prog(frac: float, base=base, size=a.size) -> None:
                prog.update(store, transcribe=round((base + frac * size) / total, 3))

            per_channel[ch] = transcribe_channel(
                a, engine, mode=mode, allowed=cfg.stt.allowed_languages,
                arabic_threshold=cfg.stt.arabic_threshold, prompt=cfg.stt_prompt(),
                max_segment_seconds=cfg.stt.max_segment_seconds,
                extra_hallucinations=cfg.stt.extra_hallucination_phrases, progress=on_prog, vad=cfg.stt.vad,
            )
            done_samples += a.size

        engine_name = getattr(engine, "name", "?")
        del engine
        if self.release_stt:
            self.release_stt()
        self._speakers(m, prog, audio, per_channel, stt_engine=engine_name)

    def _load_audio(self, m: dict) -> dict[str, np.ndarray]:
        if (self.store.get_meeting(m["id"]) or m).get("audio_deleted_at"):
            raise RuntimeError("The audio of this meeting was deleted (retention settings); the transcript and "
                               "notes are kept")
        mdir = self.cfg.audio_dir / m["id"]
        data_key = self.keystore.unwrap(self.store.get_wrapped_key(m["id"]))
        channels = list_channels(mdir)
        if not channels:
            raise RuntimeError("No audio was recorded for this meeting")
        return {ch: read_channel(mdir, ch, data_key) for ch in channels}

    def _redetect(self, m: dict, prog: Progress, num_speakers: int | None) -> None:
        audio = self._load_audio(m)
        per_channel: dict[str, list[dict]] = {}
        named = {sp["id"]: (sp["display_name"], sp.get("person_id")) for sp in self.store.get_speakers(m["id"])
                 if sp["name_source"] == "manual" and not sp["is_self"]}
        previous = {}  # line id -> (name the user gave its speaker, line length, People link)
        for s in self.store.get_segments(m["id"]):
            if s["speaker_id"] in named:
                name, pid = named[s["speaker_id"]]
                previous[s["id"]] = (name, s["end_ms"] - s["start_ms"], pid)
            seg = {k: s[k] for k in ("id", "start_ms", "end_ms", "text", "language", "confidence", "edited")}
            per_channel.setdefault(s["channel"] or "system", []).append(seg)
        if not per_channel:
            raise RuntimeError("This meeting has no transcript yet")
        info = m.get("stt_info") or {}
        self._speakers(m, prog, audio, per_channel, stt_engine=info.get("engine", "?"), num_speakers=num_speakers,
                       keep_ids=True, previous=previous)

    def _speakers(self, m: dict, prog: Progress, audio: dict[str, np.ndarray], per_channel: dict[str, list[dict]],
                  *, stt_engine: str, num_speakers: int | None = None, keep_ids: bool = False,
                  previous: dict[str, tuple] | None = None) -> None:
        cfg, store, mid = self.cfg, self.store, m["id"]
        prog.state["steps"].update({"transcribe": "done", "speakers": "running"})
        prog.update(store, step="speakers", transcribe=1.0)
        diarizer = self.diarizer_factory()
        speakers, segments = label_speakers(
            audio, per_channel, diarizer, online=m.get("mode", "online") == "online", num_speakers=num_speakers,
            min_speakers=cfg.diarization.min_speakers, max_speakers=cfg.diarization.max_speakers)
        if not keep_ids:
            for i, s in enumerate(segments):
                s["id"] = f"s{i + 1:04d}"
        if previous:
            carry_names(speakers, segments, previous)
        me = store.self_person(create=False)
        if me:  # you are always your People entry, so your name shows in every meeting
            for sp in speakers:
                if sp.get("is_self"):
                    sp.update(person_id=me["id"], display_name=me["display_name"])
        self._voiceprints(m, audio, speakers, segments)
        store.replace_speakers(mid, speakers)
        store.replace_segments(mid, segments)
        info = {"engine": stt_engine, "diarizer": getattr(diarizer, "name", "?"), "channels": sorted(audio)}
        if num_speakers:
            info["num_speakers"] = num_speakers
        store.update_meeting(mid, stt_info=info)
        prog.state["steps"]["speakers"] = "done"
        prog.update(store)

    def _voiceprints(self, m: dict, audio: dict[str, np.ndarray], speakers: list[dict], segments: list[dict]) -> None:
        """Recognise enrolled people by voice, and keep a sample of yours if you opted in. Never fatal."""
        from modwen.voiceprints import VoiceBank, enroll, recognise

        cfg, store = self.cfg, self.store
        online = m.get("mode", "online") == "online"
        bank = VoiceBank(store, self.keystore)
        me = next((sp for sp in speakers if sp.get("is_self") and sp.get("channel") == "mic"), None)
        learn_me = cfg.diarization.remember_my_voice and online and me is not None
        if self.embedder_factory is None or (bank.empty() and not learn_me):
            return
        try:
            embedder = self.embedder_factory()
            if embedder is None:
                return
            people = {p["id"]: p for p in store.list_persons()}
            recognise(bank, embedder, audio, speakers, segments, people, online=online,
                      threshold=cfg.diarization.voiceprint_threshold, margin=cfg.diarization.voiceprint_margin,
                      suggest=cfg.diarization.voiceprint_suggest, rejects=store.voice_rejects(m["id"]))
            if learn_me:
                me_person = store.self_person()
                if enroll(bank, embedder, audio, segments, me, me_person["id"], m["id"])[0]:
                    me["person_id"] = me_person["id"]
        except Exception as exc:
            log.warning("Voiceprints skipped for %s: %s", m["id"], exc)

    def _notes_with_recovery(self, m: dict, prog: Progress) -> dict:
        from modwen.notes.llm_client import LLMError

        try:
            return self._notes(m, prog)
        except LLMError as exc:
            if "Compute error" not in str(exc):
                raise
            if self.recover_llm and self.recover_llm():  # raises with a clear message if a restart can't help
                try:
                    return self._notes(m, prog)
                except LLMError as again:
                    exc = again
            raise LLMError("The local AI model ran out of GPU memory. Close other heavy apps (or pick a smaller "
                           "model in Settings → AI model), then retry. The transcript is saved.") from exc

    def _notes(self, m: dict, prog: Progress) -> dict:
        store = self.store
        mid = m["id"]
        m = store.get_meeting(mid) or m
        segments = store.get_segments(mid)
        engine = self.notes_engine_factory(m.get("llm_profile"))

        def on_part(done: int, total: int) -> None:
            prog.update(store, notes_part=done, notes_parts=total)

        result = engine.generate(
            title=m["title"],
            started_at=m.get("started_at"),
            segments=segments,
            bookmarks=m.get("bookmarks") or [],
            summary_language=m.get("summary_language") or self.cfg.notes.summary_language,
            progress=on_part,
        )
        saved = store.save_notes(mid, result.data, llm_profile=result.profile, model=result.model,
                                 prompt_version=result.prompt_version)
        store.update_meeting(mid, llm_profile=result.profile)
        return saved

    def _notify_ready(self, m: dict, notes: dict | None) -> None:
        if not self.notifier:
            return
        data = (notes or {}).get("data", {})
        dur = (self.store.get_meeting(m["id"]) or m).get("duration_ms") or 0
        h, rem = divmod(int(dur / 1000), 3600)
        mins = rem // 60
        dur_txt = f"{h}h {mins:02d}m" if h else f"{mins} min" if mins else f"{int(dur / 1000)} s"
        body = f"{dur_txt} · {len(data.get('decisions', []))} decisions · {len(data.get('action_items', []))} action items"
        self.notifier.notify(f"Notes ready: {m['title']}", body, m["id"])


def silence(seconds: float) -> np.ndarray:  # small helper for tests/demos
    return np.zeros(int(seconds * 16000), dtype=np.float32)
