"""Re-transcribe a recorded meeting with a chosen Whisper model (no LLM) to compare accuracy.

Usage (from the project folder):
  uv run python scripts/compare_stt.py                 # latest meeting, model from config
  uv run python scripts/compare_stt.py --model large-v3 --meeting <id> [--language ar]
"""

from __future__ import annotations

import argparse
import time

from modwen.audio.chunks import list_channels, read_channel
from modwen.config import load_config
from modwen.crypto import KeyStore
from modwen.notes.engine import fmt_ts
from modwen.storage import Store
from modwen.stt.engines import build_engine, resolve_models
from modwen.stt.transcriber import transcribe_channel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting")
    ap.add_argument("--model")
    ap.add_argument("--backend")
    ap.add_argument("--language", default=None, choices=["auto", "en", "ar"])
    args = ap.parse_args()

    cfg = load_config()
    store = Store(cfg.db_path)
    m = store.get_meeting(args.meeting) if args.meeting else store.list_meetings(limit=1)[0]
    from modwen.hardware import choose_tier, detect

    backend, _, final = resolve_models(cfg, choose_tier(detect(cfg.data_dir)))
    model = args.model or final
    backend = args.backend or backend
    mode = args.language or m.get("meeting_language") or "auto"
    print(f"Meeting {m['id']} '{m['title']}' — model {model} ({backend}), language {mode}\n")

    key = KeyStore(cfg).unwrap(store.get_wrapped_key(m["id"]))
    engine = build_engine(cfg, model, backend, cfg.stt.final)
    mdir = cfg.audio_dir / m["id"]
    for ch in list_channels(mdir):
        audio = read_channel(mdir, ch, key)
        t0 = time.time()
        segs = transcribe_channel(audio, engine, mode=mode, allowed=cfg.stt.allowed_languages,
                                  arabic_threshold=cfg.stt.arabic_threshold, prompt=cfg.stt_prompt(),
                                  max_segment_seconds=cfg.stt.max_segment_seconds, vad=cfg.stt.vad)
        print(f"--- channel {ch}: {len(segs)} segments, {audio.size / 16000:.0f} s audio, {time.time() - t0:.1f} s ---")
        for s in segs:
            print(f"[{fmt_ts(s['start_ms'])}] ({s['language']}) {s['text']}")
        print()


if __name__ == "__main__":
    main()
