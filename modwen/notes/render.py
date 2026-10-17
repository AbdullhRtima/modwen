"""Render notes as Markdown (Copy as Markdown / Export .md)."""

from __future__ import annotations

from modwen.notes.engine import fmt_ts


def _seg_time(seg_by_id: dict, ids: list[str]) -> str:
    times = [fmt_ts(seg_by_id[i]["start_ms"]) for i in ids if i in seg_by_id]
    return f" _(at {times[0]})_" if times else ""


def notes_markdown(meeting: dict, notes: dict | None, segments: list[dict], *, language: str | None = None,
                   include_quotes: bool = True, actions_only: bool = False) -> str:
    seg_by_id = {s["id"]: s for s in segments}
    data = (notes or {}).get("data", {})
    dur = int((meeting.get("duration_ms") or 0) / 1000)
    h, m = dur // 3600, dur % 3600 // 60
    duration = f"{h}h {m:02d}m" if h else f"{m} min" if m else f"{dur} s"
    date = (meeting.get("started_at") or "")[:10]
    lines: list[str] = []

    def actions() -> None:
        lines.append("## Action items\n")
        items = data.get("action_items", [])
        if not items:
            lines.append("_None._\n")
            return
        lines.append("| Owner | Task | Due |")
        lines.append("|---|---|---|")
        for a in items:
            due = a.get("due_date") or (f"“{a['due_raw']}”" if a.get("due_raw") else "")
            task = a["task"].replace("|", "\\|")
            lines.append(f"| {a.get('owner') or '?'} | {task} | {due} |")
        lines.append("")

    if actions_only:
        lines.append(f"# Action items: {meeting['title']} ({date})\n")
        actions()
        return "\n".join(lines)

    lines.append(f"# Meeting notes: {meeting['title']}\n")
    lines.append(f"{date} · {duration}\n")
    summary = data.get("summary", {})
    langs = [language] if language in ("en", "ar") else [k for k in ("en", "ar") if summary.get(k)]
    for lang in langs:
        if summary.get(lang):
            lines.append("## Summary\n" if lang == "en" else "## الملخص\n")
            lines.append(summary[lang].strip() + "\n")

    def points(title: str, key: str, field: str = "text", orig: str = "original_text") -> None:
        items = data.get(key, [])
        if not items:
            return
        lines.append(f"## {title}\n")
        for it in items:
            lines.append(f"- {it[field]}{_seg_time(seg_by_id, it.get('evidence_segment_ids', []))}")
            if include_quotes and it.get(orig) and it[orig] != it[field]:
                lines.append(f"  > {it[orig]}")
        lines.append("")

    points("Decisions", "decisions")
    actions()
    points("Key points", "key_points")
    points("Open questions", "open_questions", "question", "question_original")
    lines.append("---\n_Generated locally by Modwen. Nothing left this computer._")
    return "\n".join(lines)
