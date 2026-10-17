"""Dry run: re-process a recorded meeting with the current code and print transcript + notes.

The meeting is copied into a scratch database (.tmp/verify.db); your saved meeting and edits are untouched.
  uv run python scripts/verify_meeting.py <meeting_id> [--rename spk1=Sara]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

from modwen.config import load_config
from modwen.notes.engine import fmt_ts
from modwen.notes.render import notes_markdown
from modwen.services import Services
from modwen.storage import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("meeting")
    ap.add_argument("--rename", action="append", default=[], help="speaker_id=Name, applied after processing")
    args = ap.parse_args()

    cfg = load_config()
    cfg.notify.desktop = False
    scratch = Path(".tmp/verify.db")
    scratch.unlink(missing_ok=True)
    src = sqlite3.connect(cfg.db_path)
    src.row_factory = sqlite3.Row
    row = dict(src.execute("SELECT * FROM meetings WHERE id = ?", (args.meeting,)).fetchone())
    src.close()

    store = Store(scratch)
    store._conn.execute(f"INSERT INTO meetings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
    store._conn.commit()

    services = Services(cfg, start_worker=False)
    services.store = store
    services.processor.store = store
    t0 = time.time()
    services.processor.process(args.meeting)
    took = time.time() - t0
    for r in args.rename:
        sid, name = r.split("=", 1)
        store.rename_speaker(args.meeting, sid, name)

    m = store.get_meeting(args.meeting)
    segs = store.get_segments(args.meeting)
    print(f"status={m['status']}  processing took {took:.0f}s  engine={(m.get('stt_info') or {}).get('engine')}\n")
    print("--- Transcript ---")
    for s in segs:
        print(f"[{fmt_ts(s['start_ms'])}] {s['speaker_name']} ({s['language']}, c={s['confidence']:.2f}): {s['text']}")
    print()
    print(notes_markdown(m, store.latest_notes(args.meeting), segs))
    shutil.copy(scratch, ".tmp/verify-last.db")


if __name__ == "__main__":
    main()
