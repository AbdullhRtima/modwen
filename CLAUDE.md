# Modwen: notes for AI coding assistants

Modwen (مُدوَّن) is a local-first, open-source tool that turns speech into something you can use: meeting notes today, and live slides and charts next. See `docs/system-design.md`.

## Rules
- **Local only at runtime.** Never send meeting audio or transcripts to a cloud service. LLM endpoints must be loopback (`modwen/egress.py`); keep it that way.
- Keep caches, virtualenvs, model downloads and temp files inside the repository (`.venv/`, `.cache/`, `.data/`, `.tmp/`). All of these are git-ignored.
- Never commit recordings, `.data/`, models, `config.yaml` or secrets.

## Conventions
- Python 3.11–3.12, managed with `uv` (`uv sync --extra stt --extra desktop [--extra mac]`, `uv run ...`). Source in `modwen/`, tests in `tests/` (`uv run pytest`; no network or models needed).
- Web UI in `ui/` (React + Vite + TypeScript): `cd ui && npm install && npm run build`. The build output (`ui/dist`) is served by the Python app.
- Docs in `docs/`; diagrams use Mermaid fences. Bump `PROMPT_VERSION` in `modwen/notes/prompts.py` whenever prompts change.
