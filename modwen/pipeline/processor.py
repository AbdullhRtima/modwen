"""Post-meeting processing: final STT -> diarization -> merge -> names -> notes -> notification."""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from modwen.audio.chunks import list_channels, read_channel
from modwen.pipeline.merge import assign_speakers, dedupe_echo, mark_overlaps
from modwen.storage import now_iso
from modwen.stt.transcriber import transcribe_channel

log = logging.getLogger(__name__)

STEP_WEIGHTS = {"transcribe": 0.55, "speakers": 0.15, "notes": 0.30}


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
        self.state.update(changes)
        done = (STEP_WEIGHTS["transcribe"] * self.state.get("transcribe", 0)
                + (STEP_WEIGHTS["speakers"] if self.state["steps"]["speakers"] == "done" else 0)
                + STEP_WEIGHTS["notes"] * (self.state["notes_part"] / max(1, self.state["notes_parts"])
                                           if self.state["notes_parts"] else 0))
        elapsed = time.time() - self.state["started"]
        self.state["overall"] = round(done, 3)
        self.state["eta_seconds"] = int(elapsed / done - elapsed) if done > 0.05 else None
        store.update_meeting(self.meeting_id, progress=self.state)
        if self.publish:
            self.publish({"type": "progress", "meeting_id": self.meeting_id, "progress": self.state})


class Processor:
    """Runs the pipeline for one meeting. Engines are injected so tests can use fakes."""

    def __init__(self, cfg, store, keystore, *, stt_engine_factory, diarizer_factory, notes_engine_factory,
                 notifier=None, publish: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.keystore = keystore
        self.stt_engine_factory = stt_engine_factory
        self.diarizer_factory = diarizer_factory
        self.notes_engine_factory = notes_engine_factory
        self.notifier = notifier
        self.publish = publish

    # ------------------------------------------------------------------
    def process(self, meeting_id: str, *, skip_transcription: bool = False) -> dict | None:
        store = self.store
        m = store.get_meeting(meeting_id)
        if not m:
            raise KeyError(meeting_id)
        prog = Progress(meeting_id, self.publish)
        store.update_meeting(meeting_id, status="processing", error=None)
        self._emit({"type": "status", "meeting_id": meeting_id, "status": "processing"})
        try:
            if not skip_transcription:
                self._transcribe_and_merge(m, prog)
            prog.state["steps"].update({"transcribe": "done", "speakers": "done", "notes": "running"})
            prog.update(store, step="notes", transcribe=1.0)
            notes = self._notes(m, prog)
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
        mdir = cfg.audio_dir / mid
        data_key = self.keystore.unwrap(store.get_wrapped_key(mid))
        channels = list_channels(mdir)
        if not channels:
            raise RuntimeError("No audio was recorded for this meeting")
        audio = {ch: read_channel(mdir, ch, data_key) for ch in channels}
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

        prog.state["steps"].update({"transcribe": "done", "speakers": "running"})
        prog.update(store, step="speakers", transcribe=1.0)

        diarizer = self.diarizer_factory()
        speakers: list[dict] = []
        segments: list[dict] = []
        online = m.get("mode", "online") == "online"

        if "mic" in per_channel and online:
            speakers.append({"id": "you", "label": "SELF", "display_name": "You", "channel": "mic",
                             "is_self": 1, "name_source": "self_channel", "confidence": 1.0})
        if "mic" in per_channel and "system" in per_channel:
            per_channel["mic"] = dedupe_echo(per_channel["mic"], per_channel["system"])

        label_to_id: dict[str, str] = {}
        counter = 0
        for ch, segs in per_channel.items():
            if ch == "mic" and online:
                for s in segs:
                    s["speaker_id"] = "you"
                    s["channel"] = ch
                segments.extend(segs)
                continue
            turns = diarizer.diarize(audio[ch], cfg.diarization.min_speakers, cfg.diarization.max_speakers)
            assign_speakers(segs, turns)
            used = sorted({s["speaker_label"] for s in segs})
            single = len(used) <= 1 and getattr(diarizer, "name", "") == "single"
            for lab in used:
                key = f"{ch}:{lab}"
                if key not in label_to_id:
                    counter += 1
                    sid = f"spk{counter}"
                    label_to_id[key] = sid
                    name = ("Others" if online else "Room") if single else f"Speaker {counter + (1 if online else 0)}"
                    speakers.append({"id": sid, "label": lab, "display_name": name, "channel": ch, "is_self": 0,
                                     "name_source": None, "confidence": 0.0})
            for s in segs:
                s["speaker_id"] = label_to_id[f"{ch}:{s['speaker_label']}"]
                s["channel"] = ch
            segments.extend(segs)

        segments = mark_overlaps(segments)
        for i, s in enumerate(segments):
            s["id"] = f"s{i + 1:04d}"
        store.replace_speakers(mid, speakers)
        store.replace_segments(mid, segments)
        store.update_meeting(mid, stt_info={"engine": getattr(engine, "name", "?"),
                                            "diarizer": getattr(diarizer, "name", "?"),
                                            "channels": channels})
        prog.state["steps"]["speakers"] = "done"
        prog.update(store)

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
