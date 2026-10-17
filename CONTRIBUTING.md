# Contributing to Modwen

Thanks for helping. Modwen's promise is simple: **speech in, useful output out, nothing leaves the machine.** Changes should keep that promise.

## Setup

```bash
uv sync --extra stt --extra desktop            # add --extra mac on Apple Silicon, --extra windows on Windows
(cd ui && npm install && npm run build)
uv run pytest                                  # no network or models needed
uv run ruff check modwen tests
```

## Ground rules

- **Local only.** Never add code that sends audio or transcripts off the machine. LLM and STT endpoints must pass `modwen/egress.py`. A future "public data" feature (e.g. fetching a gold price for a chart) must be opt-in per request, visible in the UI, and must never include meeting content.
- **No recordings or secrets in git.** `.data/`, `config.yaml`, models and generated samples are ignored; keep it that way.
- **Tests with every change.** The suite uses fakes (a fake Whisper engine and a fake OpenAI-compatible server), so it runs in seconds.
- **Prompts are versioned.** Change `PROMPT_VERSION` in `modwen/notes/prompts.py` when you edit prompts, and describe the before/after on a real (or synthetic) transcript in the PR.

## Especially valuable

- **Arabic dialect evaluation data:** short recordings (with consent) plus reference transcripts. Gulf, Levantine, Egyptian, Maghrebi and code-switching are all welcome.
- **Windows testing:** WASAPI loopback, CUDA, notifications.
- The voice → slides/charts layer (see the roadmap in the README).

## Project layout

```
modwen/          Python core: FastAPI service, pipeline, CLI (entry point: modwen.cli:main)
  audio/         capture sources, encrypted chunks, VAD
  stt/           Whisper engines, language rule, hallucination filter
  notes/         LLM client, prompts, map-reduce notes engine, deadline resolver, Markdown export
  pipeline/      recorder, live captions, post-meeting processor
  native/macos/  Core Audio tap helper (Swift)
ui/              React + Vite web UI
tests/           pytest suite
docs/            design docs, guides and mockups
```

By contributing, you agree that your contributions are licensed under the Apache-2.0 license.
