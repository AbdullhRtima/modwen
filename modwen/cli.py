"""Command line: ``modwen serve | doctor | process | simulate | llm-test | models | init-config``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from modwen import __version__
from modwen.config import default_config_path, load_config, save_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "faster_whisper", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------------------------
def cmd_serve(args) -> int:
    import secrets

    import uvicorn

    from modwen.api import create_app
    from modwen.services import Services

    cfg = load_config(args.config)
    if args.port:
        cfg.app.ui_port = args.port
    # POSIX: block quit signals in every thread and handle them in a dedicated thread, so they work even while
    # the tray's native event loop owns the main thread (macOS). Must happen before any thread starts.
    import signal

    quit_signals = {getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)}
    use_sigwait = hasattr(signal, "pthread_sigmask") and hasattr(signal, "sigwait")
    if use_sigwait:
        signal.pthread_sigmask(signal.SIG_BLOCK, quit_signals)
    services = Services(cfg)
    services.start_managed_llm()
    import atexit

    atexit.register(services.llm_manager.stop)

    def _shutdown(*_):  # terminal closed / Ctrl+C / kill: stop recording safely and the managed AI model
        try:
            if services.recorder.session:
                services.recorder.stop()  # flushes the last audio chunk; processing resumes on next start
        except Exception:
            pass
        services.llm_manager.stop()
        os._exit(0)

    if use_sigwait:
        def _wait_for_quit():
            signal.sigwait(quit_signals)
            _shutdown()

        threading.Thread(target=_wait_for_quit, name="quit-signals", daemon=True).start()
    else:  # Windows: Ctrl+C / Ctrl+Break; a closed console is covered by the AI-model watchdog
        for name in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), _shutdown)
    token = os.environ.get("MODWEN_TOKEN") or secrets.token_urlsafe(24)
    app = create_app(services, token=token)
    url = f"http://{cfg.app.ui_host}:{cfg.app.ui_port}"
    open_url = f"{url}/?token={token}"
    print(f"Modwen {__version__} — local only. Tier {services.plan.tier}: {services.plan.label}")
    print(f"Open: {open_url}")

    server = uvicorn.Server(uvicorn.Config(app, host=cfg.app.ui_host, port=cfg.app.ui_port, log_level="warning"))

    if args.no_tray:
        if cfg.app.open_browser and not args.no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(open_url)).start()
        server.run()
        return 0

    # Tray must own the main thread on macOS; run the server in the background.
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        from modwen.tray import run_tray

        if cfg.app.open_browser and not args.no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(open_url)).start()
        run_tray(services, open_url, stop_server=lambda: setattr(server, "should_exit", True))
    except Exception as exc:  # no tray support (headless, missing extra)
        print(f"Tray unavailable ({exc}); running without it. Press Ctrl+C to quit.")
        if cfg.app.open_browser and not args.no_browser:
            webbrowser.open(open_url)
        try:
            while t.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True
    return 0


# ----------------------------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    from modwen.audio.capture import list_input_devices, mac_tap_binary
    from modwen.hardware import choose_tier, cuda_available, detect, mlx_available
    from modwen.notes.llm_client import make_client
    from modwen.stt.engines import resolve_models

    cfg = load_config(args.config)
    info = detect(cfg.data_dir)
    plan = choose_tier(info)
    backend, live, final = resolve_models(cfg, plan)
    ok = "✓"
    bad = "✗"

    print(f"Modwen {__version__} doctor\n")
    print(f"Config     : {cfg.loaded_from or '(defaults; run `modwen init-config` to create one)'}")
    print(f"Data dir   : {cfg.data_dir}")
    print(f"Machine    : {info.os} {info.os_version} {info.arch}, {info.cpu}, {info.cores} cores, {info.ram_gb} GB RAM")
    gpu = f"{info.gpu_name} ({info.vram_gb} GB)" if info.gpu_vendor else "none detected"
    print(f"GPU        : {gpu}")
    print(f"Free disk  : {info.free_disk_gb} GB")
    print(f"\nTier {plan.tier}    : {plan.label}")
    print(f"  Whisper  : backend={backend} live={live} final={final}"
          f"  (mlx installed: {mlx_available()}, CUDA: {cuda_available()})")
    print(f"  LLM      : {plan.llm_model}, context {plan.llm_context}")
    print(f"  2-hour meeting ready in about {plan.est_2h_minutes[0]}-{plan.est_2h_minutes[1]} min after Stop")
    for w in plan.warnings:
        print(f"  ! {w}")
    print("\n  Start the local LLM with:")
    print(f"    {plan.llama_server_command()}")

    print("\nAI model (active profile):")
    prof = cfg.llm.profile()
    try:
        h = make_client(cfg).health()
    except Exception as exc:
        h = {"ok": False, "error": str(exc)}
    if h.get("ok"):
        print(f"  {ok} {cfg.llm.active} at {prof.base_url} is online; models: {', '.join(h['models'])}")
        if prof.model not in h["models"] and h["models"]:
            print(f"  ! config model '{prof.model}' not in server list; llama-server serves one model so this is "
                  "usually fine, Ollama needs an exact tag.")
    else:
        print(f"  {bad} {cfg.llm.active} at {prof.base_url} is not reachable: {h.get('error')}")
        print("    See docs/local-llm-setup.md")

    print("\nAudio:")
    devices = list_input_devices()
    default = [d for d in devices if d.get("default")]
    print(f"  Mic default : {default[0]['name'] if default else devices[:1]}")
    if sys.platform == "darwin":
        exe = mac_tap_binary()
        print(f"  System audio: {'helper built at ' + str(exe) if exe else bad + ' helper not built (bash modwen/native/macos/build.sh)'}")
    elif sys.platform == "win32":
        try:
            import pyaudiowpatch  # noqa: F401

            print(f"  System audio: {ok} WASAPI loopback available")
        except ImportError:
            print(f"  System audio: {bad} install with: uv sync --extra windows")
    present = sorted({f"{d.parent.name}:{d.name.split('--')[-1]}" for sub in ("whisper", "mlx")
                      for d in (cfg.models_dir / sub).glob("models--*")})
    print("\nSpeech models present:", ", ".join(present) or "none (run `modwen models download`)")
    try:
        import pyannote.audio  # noqa: F401

        print(f"Diarization : {ok} pyannote installed")
    except Exception:
        print("Diarization : single speaker per channel (optional: uv sync --extra diarization)")
    if args.json:
        print(json.dumps({"hardware": info.to_dict(), "plan": plan.to_dict(), "llm": h}, indent=2))
    return 0


# ----------------------------------------------------------------------------------------------
def _wait_for(services, mid: str, timeout: float) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        m = services.store.get_meeting(mid)
        prog = (m or {}).get("progress") or {}
        line = f"  {m['status']:<10} step={prog.get('step', '-'):<10} transcribe={prog.get('transcribe', 0):.0%} " \
               f"notes {prog.get('notes_part', 0)}/{prog.get('notes_parts', 0)}"
        if line != last:
            print(line, flush=True)
            last = line
        if m["status"] in ("ready", "failed"):
            return m
        time.sleep(1)
    raise TimeoutError("processing did not finish in time")


def cmd_process(args) -> int:
    from modwen.api import import_audio
    from modwen.audio.chunks import load_audio_file
    from modwen.notes.render import notes_markdown

    cfg = load_config(args.config)
    cfg.notify.desktop = not args.no_notify and cfg.notify.desktop
    if args.summary_language:
        cfg.notes.summary_language = args.summary_language
    services = _services_for_cli(cfg, args)
    audio = load_audio_file(args.file)
    print(f"Loaded {args.file} ({audio.size / 16000:.1f} s). Processing locally...")
    m = import_audio(services, audio, title=args.title or Path(args.file).stem, meeting_language=args.language,
                     channel=args.channel)
    m = _wait_for(services, m["id"], args.timeout)
    segments = services.store.get_segments(m["id"])
    if segments:
        from modwen.notes.engine import fmt_ts

        print(f"\n--- Transcript ({len(segments)} segments, engine: {(m.get('stt_info') or {}).get('engine')}) ---")
        for sg in segments:
            print(f"[{fmt_ts(sg['start_ms'])}] {sg.get('speaker_name') or '?'} ({sg['language']}): {sg['text']}")
    if m["status"] == "failed":
        print(f"\nFAILED at {m.get('error')}")
        if "notes" in (m.get("error") or ""):
            print("The transcript is saved. Start your local LLM (docs/local-llm-setup.md), then run "
                  "`modwen serve` and press Retry, or re-run this command.")
        return 1
    md = notes_markdown(m, services.store.latest_notes(m["id"]), segments)
    print("\n" + md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"\nSaved {args.out}")
    print(f"\nMeeting id: {m['id']} (open it in the UI with `modwen serve`)")
    return 0


def _services_for_cli(cfg, args):
    from modwen.services import Services

    kwargs = {}
    if getattr(args, "fake_stt", False):
        from modwen.stt.engines import FakeEngine

        kwargs["stt_engine_factory"] = lambda: FakeEngine([("en", "Fake transcript line for testing.")])
        kwargs["live_engine_factory"] = kwargs["stt_engine_factory"]
    return Services(cfg, **kwargs)


def cmd_simulate(args) -> int:
    """Play audio files through the real recorder (live captions + final pass), like a meeting."""
    from modwen.audio.capture import FileSource

    cfg = load_config(args.config)
    cfg.notify.desktop = not args.no_notify and cfg.notify.desktop
    services = _services_for_cli(cfg, args)
    sources = []
    if args.mic:
        sources.append(FileSource(args.mic, "mic", args.speed))
    if args.system:
        sources.append(FileSource(args.system, "system", args.speed))
    if not sources:
        print("Give --mic and/or --system audio files")
        return 2
    printing = _printing_publish(services.bus.publish)
    services.recorder.publish = printing
    services.processor.publish = printing
    m = services.recorder.start(args.title, mode="online", meeting_language=args.language,
                                consent={"confirmed": True, "method": "cli"}, extra_sources=sources,
                                hardware_tier=services.plan.tier)
    print(f"Recording simulated meeting {m['id']} at {args.speed}x ...")
    while not all(s.finished.is_set() for s in sources):
        time.sleep(0.5)
    time.sleep(1.5)
    services.recorder.stop()
    m = _wait_for(services, m["id"], args.timeout)
    from modwen.notes.render import notes_markdown

    print("\n" + notes_markdown(m, services.store.latest_notes(m["id"]), services.store.get_segments(m["id"])))
    return 0 if m["status"] == "ready" else 1


def _printing_publish(orig):
    def publish(event):
        if event.get("type") == "live_line":
            ln = event["line"]
            print(f"  LIVE [{ln['start_ms'] // 1000:>5}s] {ln['speaker']}: {ln['text']}", flush=True)
        elif event.get("type") in ("notes_ready", "failed", "live_status"):
            print(f"  EVENT {event}", flush=True)
        orig(event)

    return publish


def cmd_llm_test(args) -> int:
    from modwen.api import run_llm_test
    from modwen.services import Services

    cfg = load_config(args.config)
    services = Services(cfg, start_worker=False)
    res = run_llm_test(services, args.profile or cfg.llm.active)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_models(args) -> int:
    from modwen.hardware import choose_tier, detect
    from modwen.stt.engines import resolve_models

    cfg = load_config(args.config)
    plan = choose_tier(detect(cfg.data_dir))
    backend, live, final = resolve_models(cfg, plan)
    names = [args.model] if args.model else sorted({live, final})
    from modwen.runtime import whisper_models

    for name in names:
        if whisper_models.is_present(cfg, name, backend):
            print(f"Whisper '{name}' ({backend}) already downloaded")
            continue
        print(f"Downloading Whisper '{name}' for {backend} into {cfg.models_dir} ...")
        last = {"pct": -1}

        def show(done=None, total=None, step=None):
            if total:
                pct = int(done * 100 / total)
                if pct != last["pct"] and pct % 5 == 0:
                    last["pct"] = pct
                    print(f"  {pct:3d}%  {done / 1e9:.2f} / {total / 1e9:.2f} GB", flush=True)

        whisper_models.download(cfg, name, backend, show)
        print(f"  done: {name}")
    _download_diarization(cfg)
    print("Models are local now; Modwen runs offline from here on.")
    return 0


def _download_diarization(cfg) -> None:
    """Cache the pyannote model too, if the optional extra is installed and a token is saved."""
    try:
        import pyannote.audio  # noqa: F401
    except Exception:
        print("Diarization: pyannote not installed (optional: uv sync --extra diarization); skipping.")
        return
    from modwen.config import resolve_secret
    from modwen.diarize import PyannoteDiarizer

    token = resolve_secret(cfg.diarization.hf_token, cfg)
    if not token:
        print("Diarization: no Hugging Face token saved. Run `modwen secret set huggingface` "
              f"after accepting the terms of {cfg.diarization.model} on huggingface.co.")
        return
    print(f"Downloading {cfg.diarization.model} ...")
    PyannoteDiarizer(cfg.diarization.model, token, cache_dir=str(cfg.models_dir / "pyannote"))
    print("  done: diarization model")


def cmd_secret(args) -> int:
    import getpass

    from modwen.crypto import SecretStore

    cfg = load_config(args.config)
    store = SecretStore(cfg)
    if args.action == "set":
        value = getpass.getpass(f"Value for '{args.name}' (input hidden): ").strip()
        if not value:
            print("Nothing entered; unchanged.")
            return 1
        store.set(args.name, value)
        print(f"Saved '{args.name}' in the {'OS keychain' if store.mode != 'file' else 'data-dir secret file'}.")
    elif args.action == "delete":
        store.delete(args.name)
        print(f"Deleted '{args.name}'.")
    else:
        print(f"'{args.name}' is {'set' if store.get(args.name) else 'not set'}.")
    return 0


def cmd_llm(args) -> int:
    """Install / start the managed local AI model without the UI."""
    from modwen.config import save_config
    from modwen.hardware import choose_tier, detect
    from modwen.runtime.llm_manager import MODEL_CHOICES, TIER_MODEL, ManagedLLM, default_variant

    cfg = load_config(args.config)
    plan = choose_tier(detect(cfg.data_dir))
    mgr = ManagedLLM(cfg)
    if args.action == "install":
        choice = args.model or TIER_MODEL[plan.tier]
        repo, gguf, alias, ctx, gb = MODEL_CHOICES[choice]
        print(f"Installing llama.cpp + {gguf} (~{gb} GB) for Tier {plan.tier} ...")
        state = {"step": ""}

        def show(done=None, total=None, step=None, force=False):
            if step and step != state["step"]:
                state["step"] = step
                print(f"  {step}", flush=True)
            if total:
                pct = int(done * 100 / total)
                if pct % 10 == 0 and state.get("pct") != (state["step"], pct):
                    state["pct"] = (state["step"], pct)
                    print(f"    {pct}%", flush=True)

        mgr.install(choice, args.variant or default_variant(plan), show)
        print(f"Saved config: {save_config(cfg)}")
        print("Done. `modwen serve` now starts the AI model automatically.")
        return 0
    if args.action == "start":
        print(mgr.start())
        print("Running. Press Ctrl+C to stop.")
        try:
            while mgr.proc and mgr.proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            mgr.stop()
        return 0
    print(json.dumps(mgr.status(), indent=2))
    return 0


def cmd_init_config(args) -> int:
    from modwen.config import Config

    target = Path(args.path) if args.path else default_config_path()
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    save_config(Config(), target)
    print(f"Wrote {target}")
    return 0


# ----------------------------------------------------------------------------------------------
def _drop_foreign_dyld_paths() -> None:
    """macOS: DYLD_* variables (e.g. set by MATLAB) make Python load the wrong libcrypto and crash on
    `import ssl`. Modwen never needs them, so restart once without them."""
    if sys.platform != "darwin" or os.environ.get("MODWEN_DYLD_CLEANED"):
        return
    keys = [k for k in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
                        "DYLD_FRAMEWORK_PATH") if os.environ.get(k)]
    if not keys:
        return
    env = {k: v for k, v in os.environ.items() if k not in keys}
    env["MODWEN_DYLD_CLEANED"] = "1"
    print(f"Note: ignoring {', '.join(keys)} for Modwen (it points Python at other apps' libraries).",
          file=sys.stderr)
    os.execve(sys.executable, [sys.executable, "-m", "modwen", *sys.argv[1:]], env)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        _drop_foreign_dyld_paths()
    p = argparse.ArgumentParser(prog="modwen", description="Modwen: local meeting notes")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the app (tray + web UI)")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--no-tray", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("doctor", help="check hardware tier, models, LLM and audio")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("process", help="process an audio file end to end (no live meeting needed)")
    sp.add_argument("file")
    sp.add_argument("--title")
    sp.add_argument("--language", choices=["auto", "en", "ar"], default="auto")
    sp.add_argument("--channel", choices=["system", "mic", "room"], default="system")
    sp.add_argument("--summary-language", choices=["en", "ar", "both"])
    sp.add_argument("--out", help="also save the notes as Markdown here")
    sp.add_argument("--timeout", type=float, default=4 * 3600)
    sp.add_argument("--no-notify", action="store_true")
    sp.add_argument("--fake-stt", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_process)

    sp = sub.add_parser("simulate", help="replay audio files through the recorder like a live meeting")
    sp.add_argument("--mic")
    sp.add_argument("--system")
    sp.add_argument("--title", default="Simulated meeting")
    sp.add_argument("--language", choices=["auto", "en", "ar"], default="auto")
    sp.add_argument("--speed", type=float, default=4.0)
    sp.add_argument("--timeout", type=float, default=4 * 3600)
    sp.add_argument("--no-notify", action="store_true")
    sp.add_argument("--fake-stt", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("llm-test", help="send a short bilingual prompt to the active LLM profile")
    sp.add_argument("--profile")
    sp.set_defaults(func=cmd_llm_test)

    sp = sub.add_parser("models", help="download the Whisper models for this machine's tier")
    sp.add_argument("action", choices=["download"])
    sp.add_argument("--model", help="a specific model name, e.g. large-v3, large-v3-turbo, small, tiny")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("llm", help="install/start/status of the managed local AI model (llama.cpp + Qwen)")
    sp.add_argument("action", choices=["install", "start", "status"])
    sp.add_argument("--model", choices=["qwen3-4b", "qwen3-8b", "qwen3-14b", "qwen3-30b-a3b"])
    sp.add_argument("--variant", choices=["macos", "cuda", "vulkan", "cpu"])
    sp.set_defaults(func=cmd_llm)

    sp = sub.add_parser("secret", help="store a secret (e.g. the Hugging Face token) in the OS keychain")
    sp.add_argument("action", choices=["set", "delete", "check"])
    sp.add_argument("name", help="e.g. huggingface")
    sp.set_defaults(func=cmd_secret)

    sp = sub.add_parser("init-config", help="write a config.yaml with all defaults")
    sp.add_argument("--path")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init_config)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
