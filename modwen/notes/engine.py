"""Notes engine: transcript -> structured notes, with map-reduce for long meetings (design §7).

Two-step generation (better Arabic): (a) extract items as JSON in English with original quotes and
evidence ids; (b) write the summary in the requested language(s) from those items only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Callable

from modwen.notes import prompts
from modwen.notes.dates import find_deadline, grounded, meeting_date, resolve
from modwen.notes.llm_client import LLMClient, LLMError, LLMTruncated
from modwen.notes.schema import ExtractResult, LLMAction, SummaryResult
from modwen.stt.language import arabic_share, has_cjk

log = logging.getLogger(__name__)


@dataclass
class NotesResult:
    data: dict
    profile: str
    model: str
    prompt_version: str = prompts.PROMPT_VERSION


def fmt_ts(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcript_line(seg: dict) -> str:
    speaker = seg.get("speaker_name") or seg.get("speaker") or "Unknown"
    return f"[{fmt_ts(seg['start_ms'])}] #{seg['id']} {speaker} ({seg.get('language') or '?'}): {seg['text']}"


def estimate_tokens(text: str) -> int:
    # Arabic costs more tokens per character than English with Qwen's tokenizer; stay conservative.
    share = arabic_share(text)
    return int(len(text) / (3.6 - 1.4 * share)) + 1


def split_windows(segments: list[dict], minutes: float, overlap_minutes: float) -> list[list[dict]]:
    if not segments:
        return []
    win = minutes * 60_000
    ov = overlap_minutes * 60_000
    start = segments[0]["start_ms"]
    end = segments[-1]["end_ms"]
    windows = []
    t = start
    while t < end:
        part = [s for s in segments if s["start_ms"] >= t - ov and s["start_ms"] < t + win]
        if part:
            windows.append(part)
        t += win
    return windows


def fit_budget(segments: list[dict], budget: int) -> list[list[dict]]:
    """Cut ``segments`` into consecutive runs whose transcript fits ``budget`` tokens (a line never splits)."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    used = 0
    for s in segments:
        cost = estimate_tokens(transcript_line(s)) + 1
        if cur and used + cost > budget:
            out.append(cur)
            cur, used = [], 0
        cur.append(s)
        used += cost
    if cur:
        out.append(cur)
    return out


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _dedupe(items: list, key: Callable) -> list:
    out: list = []
    for it in items:
        for kept in out:
            if _similar(key(it), key(kept)) >= 0.85:
                kept.evidence_segment_ids = sorted(set(kept.evidence_segment_ids) | set(it.evidence_segment_ids))
                break
        else:
            out.append(it)
    return out


class NotesEngine:
    def __init__(self, llm: LLMClient, notes_cfg, timezone_name: str = "system"):
        self.llm = llm
        self.cfg = notes_cfg
        self.tz = timezone_name

    # ------------------------------------------------------------------
    def _budget(self) -> int:
        p = self.llm.profile
        return int(p.context_window * 0.7) - p.max_output_tokens - 900  # room for the prompt text

    def _messages(self, user: str) -> list[dict]:
        return [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}]

    def _extract(self, title: str, base_date, segments: list[dict], bookmarks: list, part: tuple[int, int] | None,
                 valid_ids: set[str]) -> ExtractResult:
        transcript = "\n".join(transcript_line(s) for s in segments)
        part_note = f"This is part {part[0]} of {part[1]} of the meeting." if part and part[1] > 1 else ""
        user = prompts.EXTRACT_USER.format(
            title=title, meeting_date=base_date.isoformat(), weekday=base_date.strftime("%A"),
            part_note=part_note, bookmarks=", ".join(fmt_ts(b) for b in bookmarks) or "none", transcript=transcript,
        )

        def validate(obj: ExtractResult) -> str | None:
            cited = [i for group in (obj.key_points, obj.decisions, obj.action_items, obj.open_questions)
                     for it in group for i in it.evidence_segment_ids]
            if cited and not any(i in valid_ids for i in cited):
                return "evidence_segment_ids must be ids from the transcript lines (like s0012)."
            return None

        try:
            return self.llm.complete_json(self._messages(user), ExtractResult, validate=validate)
        except LLMTruncated:
            # Too many items for max_output_tokens: extract each half of the window separately.
            if len(segments) < 2:
                raise
            mid = len(segments) // 2
            log.info("Extract reply truncated for %d segments; splitting the window in half", len(segments))
            a = self._extract(title, base_date, segments[:mid], bookmarks, part, valid_ids)
            b = self._extract(title, base_date, segments[mid:], bookmarks, part, valid_ids)
            return ExtractResult(key_points=a.key_points + b.key_points, decisions=a.decisions + b.decisions,
                                 action_items=a.action_items + b.action_items,
                                 open_questions=a.open_questions + b.open_questions)

    def _reduce(self, title: str, parts: list[ExtractResult]) -> ExtractResult:
        """Merge window results. Too many items for one call -> merge halves first (hierarchical)."""
        if len(parts) == 1:
            return parts[0]
        merged = ExtractResult(
            key_points=[p for r in parts for p in r.key_points],
            decisions=[p for r in parts for p in r.decisions],
            action_items=[p for r in parts for p in r.action_items],
            open_questions=[p for r in parts for p in r.open_questions],
        )
        items_json = merged.model_dump_json()
        if estimate_tokens(items_json) < self._budget():
            try:
                user = prompts.REDUCE_USER.format(title=title, items=items_json)
                return self.llm.complete_json(self._messages(user), ExtractResult)
            except LLMError as exc:
                log.warning("Reduce step failed (%s); merging deterministically", exc)
        elif len(parts) > 2:
            half = len(parts) // 2
            return self._reduce(title, [self._reduce(title, parts[:half]), self._reduce(title, parts[half:])])
        merged.key_points = _dedupe(merged.key_points, lambda x: x.text)
        merged.decisions = _dedupe(merged.decisions, lambda x: x.text)
        merged.action_items = _dedupe(merged.action_items, lambda x: x.task)
        merged.open_questions = _dedupe(merged.open_questions, lambda x: x.question)
        return merged

    def _summary(self, title: str, base_date, data: dict, lang: str) -> str:
        """Summary written from the validated items (grounded deadlines, cleaned owners)."""
        compact = json.dumps({
            "decisions": [d["text"] for d in data["decisions"]],
            "action_items": [{"task": a["task"], "owner": a["owner"], "due": a["due_date"] or a["due_raw"]}
                             for a in data["action_items"]],
            "key_points": [k["text"] for k in data["key_points"]],
            "open_questions": [q["question"] for q in data["open_questions"]],
        }, ensure_ascii=False)
        template = prompts.SUMMARY_AR if lang == "ar" else prompts.SUMMARY_EN
        user = template.format(title=title, meeting_date=base_date.isoformat(), items=compact)

        def validate(obj: SummaryResult) -> str | None:
            if has_cjk(obj.summary):
                return "The summary contains Chinese/Japanese/Korean characters. Use only the requested language."
            if lang == "ar" and arabic_share(obj.summary) < 0.5:
                return "The summary must be written in Arabic."
            if lang == "en" and arabic_share(obj.summary) > 0.3:
                return "The summary must be written in English."
            if len(obj.summary.strip()) < 20:
                return "The summary is too short."
            return None

        return self.llm.complete_json(self._messages(user), SummaryResult, validate=validate,
                                      max_tokens=900).summary.strip()

    # ------------------------------------------------------------------
    def generate(self, *, title: str, started_at: str | None, segments: list[dict], bookmarks: list | None = None,
                 summary_language: str = "en", progress: Callable[[int, int], None] | None = None) -> NotesResult:
        base = meeting_date(started_at, self.tz)
        bookmarks = bookmarks or []
        valid_ids = {s["id"] for s in segments}
        warnings: list[str] = []
        langs = ["en", "ar"] if summary_language == "both" else [summary_language]

        if not segments:
            data = {"summary": {lang: "No speech was detected in this recording." for lang in langs},
                    "key_points": [], "decisions": [], "action_items": [], "open_questions": [],
                    "warnings": ["No speech detected."], "generated_at": _now(), "meeting_date": base.isoformat()}
            return NotesResult(data, self.llm.name, self.llm.profile.model)

        full = "\n".join(transcript_line(s) for s in segments)
        if estimate_tokens(full) <= self._budget():
            windows = [segments]
        else:
            windows = split_windows(segments, self.cfg.chunk_minutes, self.cfg.chunk_overlap_minutes)
            # A window can still be too dense (fast speech, a small context): split by the token budget itself,
            # so no window is ever sent larger than the model can read.
            windows = [part for w in windows for part in fit_budget(w, self._budget())]

        total_steps = len(windows) + (1 if len(windows) > 1 else 0) + len(langs)
        step = 0
        if progress:
            progress(0, total_steps)
        parts = []
        for i, w in enumerate(windows, 1):
            parts.append(self._extract(title, base, w, bookmarks, (i, len(windows)), valid_ids))
            step += 1
            if progress:
                progress(step, total_steps)
        items = parts[0] if len(parts) == 1 else self._reduce(title, parts)
        if len(parts) > 1:
            step += 1
            if progress:
                progress(step, total_steps)

        data = self._finalize(items, base, valid_ids, segments, warnings)
        summaries = {}
        for lang in langs:
            try:
                summaries[lang] = self._summary(title, base, data, lang)
            except LLMError as exc:
                warnings.append(f"Summary ({lang}) failed: {exc}")
                summaries[lang] = ""
            step += 1
            if progress:
                progress(step, total_steps)

        data["warnings"] = warnings
        data["summary"] = summaries
        data["generated_at"] = _now()
        data["meeting_date"] = base.isoformat()
        data["parts"] = len(windows)
        return NotesResult(data, self.llm.name, self.llm.profile.model)

    def _finalize(self, items: ExtractResult, base, valid_ids: set[str], segments: list[dict],
                  warnings: list[str]) -> dict:
        def ev(ids: list[str]) -> tuple[list[str], float]:
            good = [i for i in ids if i in valid_ids]
            return good, (0.9 if good else 0.3)

        def point(p, prefix: str, n: int) -> dict:
            ids, conf = ev(p.evidence_segment_ids)
            return {"id": f"{prefix}{n}", "text": p.text.strip(), "original_text": p.original_text.strip(),
                    "evidence_segment_ids": ids, "confidence": conf}

        seg_text = {s["id"]: s["text"] for s in segments}
        seg_speaker = {s["id"]: s.get("speaker_name") for s in segments}

        def action(a: LLMAction, n: int) -> dict:
            ids, conf = ev(a.evidence_segment_ids)
            due = None
            due_raw = (a.due_raw or "").strip() or None
            if due_raw and due_raw.lower() in ("null", "none", "n/a"):
                due_raw = None
            if due_raw:
                source = " ".join([seg_text.get(i, "") for i in ids] + [a.task_original or ""])
                if source.strip() and not grounded(due_raw, source):
                    # The model claimed a deadline the cited lines don't contain: re-read it from those lines.
                    log.info("Ungrounded deadline for an action item; re-reading it from the transcript")
                    found, phrase = find_deadline(source, base)
                    due_raw = phrase
                    if not phrase:
                        warnings.append(f"Dropped a deadline not found in the transcript ({a.due_raw!r})")
            if due_raw:
                resolved = resolve(due_raw, base)
                if a.due_date and a.due_raw == due_raw:
                    try:
                        due = datetime.fromisoformat(a.due_date).date().isoformat()
                    except ValueError:
                        due = None
                if resolved and (due is None or due != resolved.isoformat()):
                    # Our deterministic resolver wins for the phrases it understands.
                    due = resolved.isoformat()
            owner = (a.owner or "").strip() or None
            if owner and owner.lower() in ("null", "none", "unknown", "n/a", "everyone", "team"):
                owner = None
            if not owner:
                # Nobody named: if all cited lines come from one speaker, it's that speaker's own commitment.
                speakers = {seg_speaker.get(i) for i in ids} - {None}
                if len(speakers) == 1:
                    owner = speakers.pop()
            return {"id": f"a{n}", "task": a.task.strip(), "task_original": a.task_original.strip(),
                    "owner": owner, "due_date": due, "due_raw": due_raw,
                    "evidence_segment_ids": ids, "confidence": conf if owner else min(conf, 0.6)}

        data = {
            "key_points": [point(p, "k", i) for i, p in enumerate(items.key_points, 1)],
            "decisions": [point(p, "d", i) for i, p in enumerate(items.decisions, 1)],
            "action_items": [action(a, i) for i, a in enumerate(items.action_items, 1)],
            "open_questions": [
                {"id": f"q{i}", "question": q.question.strip(), "question_original": q.question_original.strip(),
                 "evidence_segment_ids": ev(q.evidence_segment_ids)[0], "confidence": ev(q.evidence_segment_ids)[1]}
                for i, q in enumerate(items.open_questions, 1)
            ],
        }
        data["warnings"] = warnings
        warnings[:] = refresh_warnings(data)
        no_ev = sum(1 for group in ("key_points", "decisions", "action_items", "open_questions")
                    for it in data[group] if not it["evidence_segment_ids"])
        if no_ev:
            warnings.append(f"{no_ev} item(s) without transcript evidence (low confidence)")
        data["warnings"] = warnings
        return data


def refresh_warnings(data: dict) -> list[str]:
    """Recompute the owner/evidence warnings after edits; keep other warnings (failed summaries, dropped dates)."""
    keep = [w for w in data.get("warnings", []) if "without an owner" not in w and "without transcript evidence" not in w]
    keep = [w for w in keep if "unnamed speaker" not in w]
    owners = [(a.get("owner") or "").strip() for a in data.get("action_items", [])]
    unowned = sum(1 for o in owners if not o)
    generic = sum(1 for o in owners if re.fullmatch(r"(Others|Room|Not sure|Speaker \d+|Person \d+)", o))
    if unowned:
        keep.append(f"{unowned} action item(s) without an owner")
    if generic:
        keep.append(f"{generic} action item(s) assigned to an unnamed speaker: rename speakers")
    return keep


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def retarget_line(data: dict, seg_id: str, old: str, new: str, line_speaker: dict[str, str]) -> dict:
    """A transcript line moved from speaker ``old`` to ``new``: move the notes that rest on it too.

    An action item owned by ``old`` (or a point starting "old: ...") follows the line when it cites that line
    and no other line still spoken by ``old``. ``line_speaker`` maps line ids to speaker names after the move.
    """
    if not old or old == new:
        return data

    def follows(item: dict) -> bool:
        ids = item.get("evidence_segment_ids") or []
        return seg_id in ids and not any(line_speaker.get(i) == old for i in ids if i != seg_id)

    out = dict(data)
    for group in ("action_items",):
        out[group] = [{**it, "owner": new} if (it.get("owner") or "").strip() == old and follows(it) else it
                      for it in data.get(group, [])]
    prefix = f"{old}:"
    for group in ("key_points", "decisions"):
        out[group] = [{**it, "text": new + it["text"][len(old):]}
                      if (it.get("text") or "").startswith(prefix) and follows(it) else it
                      for it in data.get(group, [])]
    out["warnings"] = refresh_warnings(out)
    return out
