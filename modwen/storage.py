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
    person_id TEXT,
    suggestion TEXT,
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
    created_at TEXT NOT NULL,
    consent_at TEXT,
    consent_meeting_id TEXT
);
CREATE TABLE IF NOT EXISTS voiceprints (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    vector BLOB NOT NULL,
    seconds REAL NOT NULL,
    meeting_id TEXT,
    speaker_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_rejects (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    segment_ids TEXT NOT NULL
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
        self._add_columns()
        self._conn.commit()

    def _migrate(self) -> None:
        """v0.1.0-dev had speakers.id as a global primary key; it must be unique per meeting only."""
        cols = self._conn.execute("PRAGMA table_info(speakers)").fetchall()
        pk = [c["name"] for c in cols if c["pk"]]
        if cols and pk == ["id"]:
            self._conn.execute("ALTER TABLE speakers RENAME TO speakers_old")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR IGNORE INTO speakers (id, meeting_id, label, display_name, channel, is_self, "
                               "name_source, confidence) SELECT id, meeting_id, label, display_name, channel, "
                               "is_self, name_source, confidence FROM speakers_old")
            self._conn.execute("DROP TABLE speakers_old")
            self._conn.commit()

    def _add_columns(self) -> None:
        """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS won't add them)."""
        added = {"meetings": [("keep", "INTEGER DEFAULT 0"), ("audio_deleted_at", "TEXT"),
                              ("transcript_deleted_at", "TEXT")],
                 "speakers": [("person_id", "TEXT"), ("suggestion", "TEXT")],
                 "segments": [("uncertain", "INTEGER DEFAULT 0")],
                 "persons": [("consent_at", "TEXT"), ("consent_meeting_id", "TEXT")]}
        for table, cols in added.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
        """Newest first, with counts for the list view: four queries in total, however many meetings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids)) or "''"
            speakers = dict(self._conn.execute(
                f"SELECT meeting_id, COUNT(*) FROM speakers WHERE meeting_id IN ({marks}) GROUP BY meeting_id",
                ids).fetchall())
            langs: dict[str, set] = {}
            for mid, lang in self._conn.execute(
                    f"SELECT DISTINCT meeting_id, language FROM segments WHERE meeting_id IN ({marks})", ids):
                if lang == "mixed":
                    langs.setdefault(mid, set()).update({"ar", "en"})
                elif lang:
                    langs.setdefault(mid, set()).add(lang)
            actions = dict(self._conn.execute(
                f"SELECT n.meeting_id, COALESCE(json_array_length(n.data, '$.action_items'), 0) FROM notes n "
                f"JOIN (SELECT meeting_id, MAX(version) v FROM notes WHERE meeting_id IN ({marks}) "
                f"GROUP BY meeting_id) last ON last.meeting_id = n.meeting_id AND last.v = n.version", ids).fetchall())
        out = []
        for r in rows:
            d = self._row(r)
            d["action_item_count"] = actions.get(d["id"], 0)  # type: ignore[index]
            d["languages"] = sorted(langs.get(d["id"], ()))  # type: ignore[index]
            d["speaker_count"] = speakers.get(d["id"], 0)  # type: ignore[index]
            out.append(d)
        return out

    def delete_meeting(self, mid: str) -> None:
        self._exec("DELETE FROM meetings WHERE id = ?", (mid,))

    def retention_candidates(self, before: str, field: str) -> list[dict]:
        """Finished, unpinned meetings older than ``before`` whose ``field`` (audio/transcript) still exists."""
        cond = {"audio": "audio_deleted_at IS NULL", "transcript": "transcript_deleted_at IS NULL",
                "meeting": "1 = 1"}[field]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, title, COALESCE(ended_at, started_at, created_at) AS t FROM meetings "
                f"WHERE COALESCE(keep, 0) = 0 AND status IN ('ready', 'failed') AND {cond} "
                f"AND COALESCE(ended_at, started_at, created_at) < ?", (before,)).fetchall()
        return [dict(r) for r in rows]

    def forget_audio(self, mid: str) -> None:
        """Crypto-shred a meeting's audio: drop its data key (the chunks are deleted by the caller)."""
        self._exec("UPDATE meetings SET wrapped_key = NULL, audio_deleted_at = ? WHERE id = ?", (now_iso(), mid))

    def forget_transcript(self, mid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM segments WHERE meeting_id = ?", (mid,))
            self._conn.execute("UPDATE meetings SET transcript_deleted_at = ? WHERE id = ?", (now_iso(), mid))
            self._conn.commit()

    # ----- speakers -----
    def replace_speakers(self, mid: str, speakers: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (mid,))
            for s in speakers:
                sug = s.get("suggestion")
                self._conn.execute(
                    "INSERT INTO speakers (id, meeting_id, label, display_name, channel, is_self, name_source, "
                    "confidence, person_id, suggestion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], mid, s["label"], s["display_name"], s.get("channel"), int(s.get("is_self", 0)),
                     s.get("name_source"), s.get("confidence", 0.0), s.get("person_id"),
                     json.dumps(sug, ensure_ascii=False) if sug else None),
                )
            self._conn.commit()

    def get_speakers(self, mid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY is_self DESC, label", (mid,)
            ).fetchall()
        out = [dict(r) for r in rows]
        for sp in out:
            sp["suggestion"] = json.loads(sp["suggestion"]) if sp.get("suggestion") else None
        return out

    def update_speaker(self, mid: str, sid: str, **fields: Any) -> None:
        allowed = {"display_name", "name_source", "confidence", "person_id", "suggestion", "is_self"}
        assert set(fields) <= allowed, fields
        if "suggestion" in fields and fields["suggestion"] is not None:
            fields["suggestion"] = json.dumps(fields["suggestion"], ensure_ascii=False)
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self._exec(f"UPDATE speakers SET {sets} WHERE meeting_id = :_m AND id = :_s", {**fields, "_m": mid, "_s": sid})

    def speaker_count(self, mid: str) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM speakers WHERE meeting_id = ?", (mid,)).fetchone()[0]

    def rename_speaker(self, mid: str, speaker_id: str, new_name: str, source: str | None = "manual") -> str | None:
        """Rename a speaker and substitute the old name in the latest notes. Returns the old name.

        Giving a speaker the name another speaker already has merges the two (one person split in two).
        ``source`` is the new ``name_source``: "manual" for a name the user typed, None for a "Person N"."""
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
                "UPDATE speakers SET display_name = ?, name_source = ?, confidence = ? WHERE meeting_id = ? AND id = ?",
                (new_name, source, 1.0 if source == "manual" else 0.0, mid, speaker_id),
            )
            self._conn.commit()
        if twin:  # keep your own speaker ("You", the mic) as the one that stays
            src, dst = (twin["id"], speaker_id) if row["is_self"] else (speaker_id, twin["id"])
            self.merge_speakers(mid, src, dst)
        self._rename_in_notes(mid, old, new_name)
        return old

    def _rename_in_notes(self, mid: str, old: str, new: str) -> None:
        notes = self.latest_notes(mid)
        if not notes or not old or old == new:
            return
        # "You" is also an ordinary word in the notes ("You should..."): replace it only where it is used as
        # the speaker's name (an owner, or "You:" at the start of a point).
        generic = old == "You"
        pattern = re.compile(r"^You(?=:)" if generic else rf"(?<!\w){re.escape(old)}(?!\w)")

        def sub(obj: Any, key: str | None = None) -> Any:
            if isinstance(obj, str):
                if generic and key == "owner":
                    return new if obj.strip() == old else obj
                return pattern.sub(new, obj)
            if isinstance(obj, list):
                return [sub(x, key) for x in obj]
            if isinstance(obj, dict):
                return {k: (v if k in ("id", "evidence_segment_ids") else sub(v, k)) for k, v in obj.items()}
            return obj

        self._exec("UPDATE notes SET data = ? WHERE id = ?", (json.dumps(sub(notes["data"]), ensure_ascii=False),
                                                              notes["id"]))

    def merge_speakers(self, mid: str, source_id: str, target_id: str) -> str:
        """Give all of ``source``'s lines to ``target`` and remove ``source``. Returns the speaker that stays.

        Your own speaker is never the one removed, a link to People survives, and the notes follow."""
        with self._lock:
            rows = {r["id"]: dict(r) for r in self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? AND id IN (?, ?)", (mid, source_id, target_id))}
        if source_id == target_id or len(rows) != 2:
            raise ValueError("Pick two different speakers of this meeting")
        if rows[source_id]["is_self"] and not rows[target_id]["is_self"]:
            source_id, target_id = target_id, source_id
        src, dst = rows[source_id], rows[target_id]
        with self._lock:
            self._conn.execute("UPDATE segments SET speaker_id = ? WHERE meeting_id = ? AND speaker_id = ?",
                               (target_id, mid, source_id))
            if src["person_id"] and not dst["person_id"]:
                self._conn.execute("UPDATE speakers SET person_id = ? WHERE meeting_id = ? AND id = ?",
                                   (src["person_id"], mid, target_id))
            self._conn.execute("DELETE FROM speakers WHERE meeting_id = ? AND id = ?", (mid, source_id))
            self._conn.commit()
        self._rename_in_notes(mid, src["display_name"], dst["display_name"])
        return target_id

    def link_speaker(self, mid: str, sid: str, pid: str) -> str:
        """Say a speaker is this person: their name everywhere in the meeting, and a link to the People page so
        renaming or deleting the person later updates the meeting. Returns the speaker id that holds the link
        (another speaker that already had the name is merged in)."""
        person = self.get_person(pid)
        if person is None:
            raise KeyError(pid)
        self.rename_speaker(mid, sid, person["display_name"])
        alive = {x["id"]: x for x in self.get_speakers(mid)}
        if sid not in alive:  # merged into the speaker that already had this name
            sid = next(x["id"] for x in alive.values() if x["display_name"].lower() == person["display_name"].lower())
        self.update_speaker(mid, sid, person_id=pid, suggestion=None, is_self=int(bool(person["is_self"])))
        return sid

    def unlink_speaker(self, mid: str, sid: str) -> None:
        """Back to an unnamed "Person N" (you stay "You")."""
        sp = next((x for x in self.get_speakers(mid) if x["id"] == sid), None)
        if sp is None:
            return
        self.rename_speaker(mid, sid, "You" if sp["is_self"] else self.next_person_name(mid), source=None)
        self.update_speaker(mid, sid, person_id=None, suggestion=None)

    def linked_speakers(self, pid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM speakers WHERE person_id = ?", (pid,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _next_person_number(rows) -> int:
        nums = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"Person (\d+)", r["display_name"]))]
        return max(nums, default=0) + 1

    def next_person_name(self, mid: str) -> str:
        with self._lock:
            rows = self._conn.execute("SELECT display_name FROM speakers WHERE meeting_id = ?", (mid,)).fetchall()
        return f"Person {self._next_person_number(rows)}"

    def add_speaker(self, mid: str, channel: str | None = None) -> dict:
        """A new, unnamed "Person N" (when the user moves a line to someone the diarizer missed)."""
        with self._lock:
            rows = self._conn.execute("SELECT id, display_name FROM speakers WHERE meeting_id = ?", (mid,)).fetchall()
            ids = {r["id"] for r in rows}
            n = self._next_person_number(rows)
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
            cur = self._conn.execute("UPDATE segments SET speaker_id = ?, uncertain = 0 WHERE meeting_id = ? AND id = ?",
                                     (speaker_id, mid, seg_id))  # the user decided: no longer uncertain
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
                    "language, confidence, overlap, edited, uncertain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], mid, i, s.get("speaker_id"), s.get("channel"), int(s["start_ms"]), int(s["end_ms"]),
                     s["text"], s.get("language"), float(s.get("confidence", 0.0)), int(s.get("overlap", 0)),
                     int(s.get("edited") or 0), int(s.get("uncertain") or 0)),
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
        """Add a person, or edit one (``id`` given). A new name reaches every meeting they are linked to.

        Raises ValueError for an empty or duplicate name, KeyError when editing someone who doesn't exist."""
        pid = person.get("id")
        fields = {k: (person.get(k) or "").strip() or None for k in ("display_name", "display_name_ar", "email")}
        name = fields["display_name"]
        if not name:
            raise ValueError("A name is required")
        with self._lock:
            old = self._conn.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone() if pid else None
            if pid and old is None and not person.get("create"):
                raise KeyError(pid)
            clash = self._conn.execute("SELECT display_name FROM persons WHERE lower(display_name) = lower(?) "
                                       "AND id != ?", (name, pid or "")).fetchone()
            if clash:
                raise ValueError(f"{clash['display_name']} is already on the People page")
            if old:
                self._conn.execute("UPDATE persons SET display_name = ?, display_name_ar = ?, email = ? WHERE id = ?",
                                   (name, fields["display_name_ar"], fields["email"], pid))
            else:
                pid = pid or uuid.uuid4().hex[:10]
                self._conn.execute(
                    "INSERT INTO persons (id, display_name, display_name_ar, email, is_self, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, name, fields["display_name_ar"], fields["email"], int(person.get("is_self", 0)), now_iso()))
            self._conn.commit()
        if old and old["display_name"] != name:
            self._relabel_person(pid, name)
        return self.get_person(pid)  # type: ignore[return-value]

    def _relabel_person(self, pid: str, name: str) -> int:
        """Show a person's (new) name on every speaker linked to them. Returns how many meetings changed."""
        meetings = set()
        for sp in self.linked_speakers(pid):
            if sp["display_name"] != name:
                self.rename_speaker(sp["meeting_id"], sp["id"], name, source=sp["name_source"] or "manual")
                meetings.add(sp["meeting_id"])
        return len(meetings)

    def delete_person(self, pid: str) -> int:
        """Remove a person, their voiceprints, and their name from every meeting (back to "Person N").

        Returns how many meetings changed. You can't delete yourself (ValueError)."""
        person = self.get_person(pid)
        if person is None:
            raise KeyError(pid)
        if person["is_self"]:
            raise ValueError("This is you; edit your name instead")
        linked = self.linked_speakers(pid)
        for sp in linked:
            self.unlink_speaker(sp["meeting_id"], sp["id"])
        self._exec("DELETE FROM persons WHERE id = ?", (pid,))  # voiceprints and rejects cascade
        return len({sp["meeting_id"] for sp in linked})

    def make_self(self, pid: str) -> dict:
        """"This is me": this person becomes you. Your previous entry (with its voiceprints and meeting links) is
        merged into it, and your speaker in every meeting shows this name."""
        person = self.get_person(pid)
        if person is None:
            raise KeyError(pid)
        with self._lock:
            old = self._conn.execute("SELECT id FROM persons WHERE is_self = 1 AND id != ?", (pid,)).fetchone()
            if old:
                for table in ("voiceprints", "voice_rejects"):
                    self._conn.execute(f"UPDATE {table} SET person_id = ? WHERE person_id = ?", (pid, old["id"]))
                self._conn.execute("UPDATE speakers SET person_id = ? WHERE person_id = ?", (pid, old["id"]))
                self._conn.execute("DELETE FROM persons WHERE id = ?", (old["id"],))
            self._conn.execute("UPDATE persons SET is_self = 1 WHERE id = ?", (pid,))
            self._conn.execute("UPDATE speakers SET person_id = ? WHERE is_self = 1", (pid,))
            self._conn.commit()
        self._relabel_person(pid, person["display_name"])
        return self.get_person(pid)  # type: ignore[return-value]

    def sync_people(self) -> None:
        """Link meeting speakers to People entries (names given before links existed, and you in every meeting)."""
        with self._lock:
            self._conn.execute(
                "UPDATE speakers SET person_id = (SELECT p.id FROM persons p WHERE p.is_self = 0 AND "
                "lower(p.display_name) = lower(speakers.display_name)) WHERE person_id IS NULL AND is_self = 0 "
                "AND name_source IN ('manual', 'voiceprint')")
            me = self._conn.execute("SELECT id, display_name FROM persons WHERE is_self = 1").fetchone()
            if me:
                self._conn.execute("UPDATE speakers SET person_id = ? WHERE is_self = 1", (me["id"],))
            self._conn.commit()
        if me:
            self._relabel_person(me["id"], me["display_name"])

    def get_person(self, pid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None

    def find_person(self, name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM persons WHERE lower(display_name) = lower(?) "
                                     "OR display_name_ar = ?", (name.strip(), name.strip())).fetchone()
        return dict(row) if row else None

    def self_person(self, create: bool = True) -> dict | None:
        """Your own entry on the People page (created on first use; edit it to set your name)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM persons WHERE is_self = 1").fetchone()
        if row or not create:
            return dict(row) if row else None
        me = self.upsert_person({"id": "self", "display_name": "You", "is_self": 1, "create": True})
        self._exec("UPDATE speakers SET person_id = ? WHERE is_self = 1", (me["id"],))  # you, in every meeting
        return me

    def record_consent(self, pid: str, mid: str | None) -> None:
        self._exec("UPDATE persons SET consent_at = COALESCE(consent_at, ?), consent_meeting_id = "
                   "COALESCE(consent_meeting_id, ?) WHERE id = ?", (now_iso(), mid, pid))

    # ----- voiceprints (vectors arrive already encrypted; see modwen/voiceprints.py) -----
    def add_voiceprint(self, pid: str, kind: str, vector: bytes, seconds: float, mid: str | None = None,
                       sid: str | None = None, keep: int = 10) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO voiceprints (id, person_id, kind, vector, seconds, meeting_id, speaker_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:12], pid, kind, vector, float(seconds), mid, sid, now_iso()))
            if mid:  # one sample per person per meeting: a newer one replaces it
                self._conn.execute(
                    "DELETE FROM voiceprints WHERE person_id = ? AND kind = ? AND meeting_id = ? AND id NOT IN "
                    "(SELECT id FROM voiceprints WHERE person_id = ? AND kind = ? AND meeting_id = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1)", (pid, kind, mid, pid, kind, mid))
            self._conn.execute(
                "DELETE FROM voiceprints WHERE person_id = ? AND kind = ? AND id NOT IN (SELECT id FROM voiceprints "
                "WHERE person_id = ? AND kind = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (pid, kind, pid, kind, keep))
            self._conn.commit()

    def list_voiceprints(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM voiceprints ORDER BY person_id, created_at").fetchall()
        return [dict(r) for r in rows]

    def voice_summary(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT person_id, kind, COUNT(*) n, SUM(seconds) secs FROM voiceprints "
                                      "GROUP BY person_id, kind").fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            v = out.setdefault(r["person_id"], {"samples": 0, "seconds": 0.0, "kinds": {}})
            v["samples"] += r["n"]
            v["seconds"] += r["secs"] or 0.0
            v["kinds"][r["kind"]] = r["n"]
        return out

    def delete_voiceprints(self, pid: str) -> None:
        self._exec("DELETE FROM voiceprints WHERE person_id = ?", (pid,))

    def add_voice_reject(self, mid: str, pid: str, segment_ids: list[str]) -> None:
        self._exec("INSERT INTO voice_rejects (meeting_id, person_id, segment_ids) VALUES (?, ?, ?)",
                   (mid, pid, json.dumps(segment_ids)))

    def voice_rejects(self, mid: str) -> list[tuple[str, set[str]]]:
        with self._lock:
            rows = self._conn.execute("SELECT person_id, segment_ids FROM voice_rejects WHERE meeting_id = ?",
                                      (mid,)).fetchall()
        return [(r["person_id"], set(json.loads(r["segment_ids"]))) for r in rows]

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
