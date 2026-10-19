"""SQLite storage for meetings, speakers, transcript segments and notes.

SQLCipher (whole-database encryption) is planned for Phase 3; audio is already encrypted per meeting.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    platform TEXT DEFAULT 'other',
    mode TEXT DEFAULT 'online',
    status TEXT NOT NULL,
    meeting_language TEXT DEFAULT 'auto',
    summary_language TEXT DEFAULT 'en',
    started_at TEXT,
    ended_at TEXT,
    timezone TEXT,
    duration_ms INTEGER DEFAULT 0,
    wrapped_key BLOB,
    channels TEXT DEFAULT '[]',
    stt_info TEXT,
    llm_profile TEXT,
    hardware_tier TEXT,
    consent TEXT,
    bookmarks TEXT DEFAULT '[]',
    progress TEXT DEFAULT '{}',
    error TEXT,
    notified_at TEXT,
    opened_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speakers (
    id TEXT NOT NULL,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    display_name TEXT NOT NULL,
    channel TEXT,
    is_self INTEGER DEFAULT 0,
    name_source TEXT,
    confidence REAL DEFAULT 0,
    PRIMARY KEY (meeting_id, id)
);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT NOT NULL,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    speaker_id TEXT,
    channel TEXT,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    language TEXT,
    confidence REAL DEFAULT 0,
    overlap INTEGER DEFAULT 0,
    edited INTEGER DEFAULT 0,
    PRIMARY KEY (meeting_id, id)
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    llm_profile TEXT,
    model TEXT,
    prompt_version TEXT,
    data TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    display_name_ar TEXT,
    email TEXT,
    is_self INTEGER DEFAULT 0,
    voiceprint TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id, seq);
CREATE INDEX IF NOT EXISTS idx_notes_meeting ON notes(meeting_id, version);
"""

JSON_FIELDS = {"channels", "stt_info", "consent", "bookmarks", "progress"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """v0.1.0-dev had speakers.id as a global primary key; it must be unique per meeting only."""
        cols = self._conn.execute("PRAGMA table_info(speakers)").fetchall()
        pk = [c["name"] for c in cols if c["pk"]]
        if cols and pk == ["id"]:
            self._conn.execute("ALTER TABLE speakers RENAME TO speakers_old")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO speakers SELECT id, meeting_id, label, display_name, channel, "
                               "is_self, name_source, confidence FROM speakers_old")
            self._conn.execute("DROP TABLE speakers_old")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----- helpers -----
    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS & d.keys():
            if d[k] is not None:
                d[k] = json.loads(d[k])
        d.pop("wrapped_key", None)
        return d

    # ----- meetings -----
    def create_meeting(self, title: str, *, wrapped_key: bytes | None = None, **fields: Any) -> dict:
        mid = fields.pop("id", None) or uuid.uuid4().hex[:12]
        row = {
            "id": mid,
            "title": title,
            "status": fields.pop("status", "recording"),
            "created_at": now_iso(),
            "started_at": fields.pop("started_at", now_iso()),
            "wrapped_key": wrapped_key,
            **fields,
        }
        for k in JSON_FIELDS & row.keys():
            row[k] = json.dumps(row[k], ensure_ascii=False)
        cols = ", ".join(row)
        qs = ", ".join(f":{k}" for k in row)
        self._exec(f"INSERT INTO meetings ({cols}) VALUES ({qs})", row)
        return self.get_meeting(mid)  # type: ignore[return-value]

    def update_meeting(self, mid: str, **fields: Any) -> None:
        if not fields:
            return
        for k in JSON_FIELDS & fields.keys():
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self._exec(f"UPDATE meetings SET {sets} WHERE id = :_id", {**fields, "_id": mid})

    def get_meeting(self, mid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE id = ?", (mid,)).fetchone()
        return self._row(row)

    def get_wrapped_key(self, mid: str) -> bytes | None:
        with self._lock:
            row = self._conn.execute("SELECT wrapped_key FROM meetings WHERE id = ?", (mid,)).fetchone()
        return row["wrapped_key"] if row else None

    def list_meetings(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = self._row(r)
            n = self.latest_notes(d["id"])  # type: ignore[index]
            d["action_item_count"] = len(n["data"].get("action_items", [])) if n else 0  # type: ignore[index]
            d["languages"] = self.meeting_languages(d["id"])  # type: ignore[index]
            d["speaker_count"] = self.speaker_count(d["id"])  # type: ignore[index]
            out.append(d)
        return out

    def delete_meeting(self, mid: str) -> None:
        self._exec("DELETE FROM meetings WHERE id = ?", (mid,))

    # ----- speakers -----
    def replace_speakers(self, mid: str, speakers: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (mid,))
            for s in speakers:
                self._conn.execute(
                    "INSERT INTO speakers (id, meeting_id, label, display_name, channel, is_self, name_source, "
                    "confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], mid, s["label"], s["display_name"], s.get("channel"), int(s.get("is_self", 0)),
                     s.get("name_source"), s.get("confidence", 0.0)),
                )
            self._conn.commit()

    def get_speakers(self, mid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY is_self DESC, label", (mid,)
            ).fetchall()
        return [dict(r) for r in rows]

    def speaker_count(self, mid: str) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM speakers WHERE meeting_id = ?", (mid,)).fetchone()[0]

    def rename_speaker(self, mid: str, speaker_id: str, new_name: str) -> str | None:
        """Rename a speaker and substitute the old name in the latest notes. Returns the old name.

        Giving a speaker the name another speaker already has merges the two (one person split in two)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT display_name, is_self FROM speakers WHERE meeting_id = ? AND id = ?", (mid, speaker_id)
            ).fetchone()
            if not row:
                return None
            old = row["display_name"]
            twin = self._conn.execute(
                "SELECT id FROM speakers WHERE meeting_id = ? AND id != ? AND lower(display_name) = lower(?)",
                (mid, speaker_id, new_name),
            ).fetchone()
            self._conn.execute(
                "UPDATE speakers SET display_name = ?, name_source = 'manual', confidence = 1.0 "
                "WHERE meeting_id = ? AND id = ?",
                (new_name, mid, speaker_id),
            )
            self._conn.commit()
        if twin:  # keep your own speaker ("You", the mic) as the one that stays
            src, dst = (twin["id"], speaker_id) if row["is_self"] else (speaker_id, twin["id"])
            self.merge_speakers(mid, src, dst)
        notes = self.latest_notes(mid)
        if notes and old and old != new_name:
            # "You" is also an ordinary word in the notes ("You should..."): replace it only where it is
            # used as the speaker's name (an owner, or "You:" at the start of a point).
            generic = old == "You"
            pattern = re.compile(r"^You(?=:)" if generic else rf"(?<!\w){re.escape(old)}(?!\w)")

            def sub(obj: Any, key: str | None = None) -> Any:
                if isinstance(obj, str):
                    if generic and key == "owner":
                        return new_name if obj.strip() == old else obj
                    return pattern.sub(new_name, obj)
                if isinstance(obj, list):
                    return [sub(x, key) for x in obj]
                if isinstance(obj, dict):
                    return {k: (v if k in ("id", "evidence_segment_ids") else sub(v, k)) for k, v in obj.items()}
                return obj

            self._exec("UPDATE notes SET data = ? WHERE id = ?",
                       (json.dumps(sub(notes["data"]), ensure_ascii=False), notes["id"]))
        return old

    def merge_speakers(self, mid: str, source_id: str, target_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE segments SET speaker_id = ? WHERE meeting_id = ? AND speaker_id = ?",
                (target_id, mid, source_id),
            )
            self._conn.execute("DELETE FROM speakers WHERE meeting_id = ? AND id = ?", (mid, source_id))
            self._conn.commit()

    def add_speaker(self, mid: str, channel: str | None = None) -> dict:
        """A new, unnamed "Person N" (when the user moves a line to someone the diarizer missed)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, display_name FROM speakers WHERE meeting_id = ?", (mid,)).fetchall()
            ids = {r["id"] for r in rows}
            nums = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"Person (\d+)", r["display_name"]))]
            n = max(nums, default=0) + 1
            k = n
            while f"spk{k}" in ids:
                k += 1
            sp = {"id": f"spk{k}", "label": "MANUAL", "display_name": f"Person {n}", "channel": channel,
                  "is_self": 0, "name_source": None, "confidence": 0.0}
            self._conn.execute(
                "INSERT INTO speakers (id, meeting_id, label, display_name, channel, is_self, name_source, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sp["id"], mid, sp["label"], sp["display_name"], channel, 0, None, 0.0),
            )
            self._conn.commit()
        return sp

    def set_segment_speaker(self, mid: str, seg_id: str, speaker_id: str) -> bool:
        """Move one transcript line to another speaker; speakers left without lines are removed."""
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM speakers WHERE meeting_id = ? AND id = ?",
                                      (mid, speaker_id)).fetchone():
                return False
            cur = self._conn.execute("UPDATE segments SET speaker_id = ? WHERE meeting_id = ? AND id = ?",
                                     (speaker_id, mid, seg_id))
            self._conn.execute(
                "DELETE FROM speakers WHERE meeting_id = ? AND is_self = 0 AND id NOT IN "
                "(SELECT DISTINCT speaker_id FROM segments WHERE meeting_id = ? AND speaker_id IS NOT NULL)",
                (mid, mid),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ----- segments -----
    def replace_segments(self, mid: str, segments: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE meeting_id = ?", (mid,))
            for i, s in enumerate(segments):
                self._conn.execute(
                    "INSERT INTO segments (id, meeting_id, seq, speaker_id, channel, start_ms, end_ms, text, "
                    "language, confidence, overlap, edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], mid, i, s.get("speaker_id"), s.get("channel"), int(s["start_ms"]), int(s["end_ms"]),
                     s["text"], s.get("language"), float(s.get("confidence", 0.0)), int(s.get("overlap", 0)),
                     int(s.get("edited") or 0)),
                )
            self._conn.commit()

    def get_segments(self, mid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, sp.display_name AS speaker_name, sp.is_self AS speaker_is_self "
                "FROM segments s LEFT JOIN speakers sp ON sp.id = s.speaker_id AND sp.meeting_id = s.meeting_id "
                "WHERE s.meeting_id = ? ORDER BY s.seq",
                (mid,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_segment_text(self, mid: str, seg_id: str, text: str) -> None:
        self._exec("UPDATE segments SET text = ?, edited = 1 WHERE meeting_id = ? AND id = ?", (text, mid, seg_id))

    def meeting_languages(self, mid: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT language, COUNT(*) c FROM segments WHERE meeting_id = ? GROUP BY language", (mid,)
            ).fetchall()
        langs = set()
        for r in rows:
            if r["language"] == "mixed":
                langs.update({"ar", "en"})
            elif r["language"]:
                langs.add(r["language"])
        return sorted(langs)

    # ----- people -----
    def list_persons(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM persons ORDER BY is_self DESC, display_name").fetchall()
        return [dict(r) for r in rows]

    def upsert_person(self, person: dict) -> dict:
        pid = person.get("id") or uuid.uuid4().hex[:10]
        fields = {k: person.get(k) for k in ("display_name", "display_name_ar", "email")}
        if not (fields["display_name"] or "").strip():
            raise ValueError("display_name is required")
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM persons WHERE id = ?", (pid,)).fetchone()
            if exists:
                self._conn.execute("UPDATE persons SET display_name = ?, display_name_ar = ?, email = ? WHERE id = ?",
                                   (fields["display_name"], fields["display_name_ar"], fields["email"], pid))
            else:
                self._conn.execute(
                    "INSERT INTO persons (id, display_name, display_name_ar, email, is_self, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, fields["display_name"], fields["display_name_ar"], fields["email"],
                     int(person.get("is_self", 0)), now_iso()))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
        return dict(row)

    def delete_person(self, pid: str) -> None:
        self._exec("DELETE FROM persons WHERE id = ? AND is_self = 0", (pid,))

    # ----- notes -----
    def save_notes(self, mid: str, data: dict, *, llm_profile: str, model: str, prompt_version: str) -> dict:
        prev = self.latest_notes(mid)
        version = (prev["version"] + 1) if prev else 1
        nid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO notes (id, meeting_id, version, llm_profile, model, prompt_version, data, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (nid, mid, version, llm_profile, model, prompt_version, json.dumps(data, ensure_ascii=False), now_iso()),
        )
        return self.latest_notes(mid)  # type: ignore[return-value]

    def update_notes_data(self, note_id: str, data: dict) -> None:
        self._exec("UPDATE notes SET data = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), note_id))

    def latest_notes(self, mid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notes WHERE meeting_id = ? ORDER BY version DESC LIMIT 1", (mid,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d
