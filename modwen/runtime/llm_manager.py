"""Managed local LLM: Modwen downloads llama.cpp's ``llama-server`` and a Qwen GGUF for this machine's tier,
then starts/stops the server itself on 127.0.0.1. Users who already run their own local server (Ollama,
LM Studio, llama.cpp) can skip this and just point a profile at its localhost URL.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx

from modwen.runtime.download import download, extract

log = logging.getLogger(__name__)

LLAMA_BUILD = "b11693"  # pinned, tested release of llama.cpp
GH_RELEASE = "https://github.com/ggml-org/llama.cpp/releases/download/{build}/{asset}"

# Tier -> (Hugging Face repo, GGUF file, alias, context, approx size GB)
MODEL_CHOICES = {
    "qwen3-4b": ("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "qwen3-4b", 16384, 2.5),
    "qwen3-8b": ("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", "qwen3-8b", 16384, 5.0),
    "qwen3-14b": ("Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf", "qwen3-14b", 32768, 9.0),
    "qwen3-30b-a3b": ("unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF", "Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
                      "qwen3-30b-a3b", 32768, 18.6),
}
TIER_MODEL = {"A": "qwen3-14b", "B": "qwen3-8b", "C": "qwen3-30b-a3b", "D": "qwen3-8b", "E": "qwen3-30b-a3b",
              "F": "qwen3-4b"}
HYBRID_THINKING = {"qwen3-8b", "qwen3-14b"}  # need enable_thinking: false


def default_variant(plan) -> str:
    """macOS -> Metal build; NVIDIA -> CUDA build; otherwise the CPU build (Vulkan is an opt-in choice)."""
    if platform.system() == "Darwin":
        return "macos"
    return "cuda" if plan.device == "cuda" else "cpu"


def llama_assets(variant: str) -> list[str]:
    """Release asset names for this OS/variant."""
    system, machine = platform.system(), platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    b = LLAMA_BUILD
    if system == "Darwin":
        return [f"llama-{b}-bin-macos-{'arm64' if arm else 'x64'}.tar.gz"]
    if system == "Windows":
        if variant == "cuda":
            return [f"llama-{b}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"]
        if variant == "vulkan":
            return [f"llama-{b}-bin-win-vulkan-x64.zip"]
        return [f"llama-{b}-bin-win-cpu-{'arm64' if arm else 'x64'}.zip"]
    if variant == "vulkan":
        return [f"llama-{b}-bin-ubuntu-vulkan-x64.tar.gz"]
    return [f"llama-{b}-bin-ubuntu-{'arm64' if arm else 'x64'}.tar.gz"]


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


class ManagedLLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self._log_fh = None

    # ----- paths -----
    @property
    def root(self) -> Path:
        p = self.cfg.data_dir / "llm"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin" / LLAMA_BUILD

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    def server_binary(self) -> Path | None:
        name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
        if self.bin_dir.exists():
            for p in self.bin_dir.rglob(name):
                return p
        return None

    def model_path(self) -> Path | None:
        m = self.cfg.llm.managed
        if not m.model_file:
            return None
        p = self.models_dir / m.model_file
        return p if p.exists() else None

    def status(self) -> dict:
        m = self.cfg.llm.managed
        running = self.proc is not None and self.proc.poll() is None
        return {
            "enabled": m.enabled,
            "model_choice": m.model_choice,
            "model_file": m.model_file,
            "binary_installed": self.server_binary() is not None,
            "model_installed": self.model_path() is not None,
            "running": running,
            "port": m.port,
            "port_busy_external": (not running) and port_open(m.port),
            "log": str(self.root / "llama-server.log"),
        }

    # ----- install -----
    def install(self, choice: str, variant: str, update) -> None:
        if choice not in MODEL_CHOICES:
            raise ValueError(f"unknown model {choice}")
        repo, gguf, alias, ctx, _ = MODEL_CHOICES[choice]
        assets = llama_assets(variant)
        if self.server_binary() is None:
            dl_dir = self.root / "downloads"
            for i, asset in enumerate(assets, 1):
                update(step=f"Downloading llama.cpp ({i}/{len(assets)}): {asset}", done=0, total=0)
                path = download(GH_RELEASE.format(build=LLAMA_BUILD, asset=asset), dl_dir / asset,
                                lambda d, t: update(done=d, total=t))
                update(step=f"Unpacking {asset}")
                extract(path, self.bin_dir)
                path.unlink(missing_ok=True)
            exe = self.server_binary()
            if exe is None:
                raise RuntimeError("llama-server not found in the downloaded archive")
            if platform.system() != "Windows":
                for p in exe.parent.iterdir():
                    if p.is_file():
                        p.chmod(p.stat().st_mode | 0o111)
        target = self.models_dir / gguf
        if not target.exists():
            update(step=f"Downloading {gguf}", done=0, total=0)
            download(f"https://huggingface.co/{repo}/resolve/main/{gguf}", target, lambda d, t: update(done=d, total=t))
        m = self.cfg.llm.managed
        m.enabled, m.model_choice, m.model_file, m.alias, m.context = True, choice, gguf, alias, ctx
        self.register_profile()
        update(step="Installed")

    def register_profile(self) -> None:
        """Add/refresh the 'managed_qwen' profile pointing at the managed server, and make it active."""
        from modwen.config import LLMProfile

        m = self.cfg.llm.managed
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if m.model_choice in HYBRID_THINKING else {}
        self.cfg.llm.profiles["managed_qwen"] = LLMProfile(
            base_url=f"http://127.0.0.1:{m.port}/v1", model=m.alias, context_window=m.context, extra_body=extra)
        self.cfg.llm.active = "managed_qwen"

    # ----- run -----
    @property
    def pid_file(self) -> Path:
        return self.root / "llama-server.pid"

    def _our_servers(self) -> list:
        """Every llama-server started from Modwen's own folder (also orphans whose watchdog is gone)."""
        import psutil

        mine = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "llama-server" in cmd and str(self.bin_dir) in cmd and p.pid != os.getpid():
                mine.append(p)
        return mine

    def _kill_leftover(self) -> None:
        """If Modwen was closed abruptly, its previous llama-server may still hold the port and GPU memory: stop
        every one from Modwen's folder. One that survives a SIGKILL is stuck in the GPU driver; only a restart
        of the computer frees it, so say that instead of failing later with "Compute error"."""
        import psutil

        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                if str(self.bin_dir) in " ".join(psutil.Process(pid).cmdline()):
                    self._kill_tree(pid)
            except (ValueError, psutil.Error):
                pass
            self.pid_file.unlink(missing_ok=True)
        leftovers = self._our_servers()
        if not leftovers:
            return
        log.info("Stopping leftover managed llama-server(s): %s", [p.pid for p in leftovers])
        for p in leftovers:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        _, stuck = psutil.wait_procs(leftovers, timeout=6)
        if stuck:
            raise RuntimeError(
                "An earlier copy of the local AI model is stuck in macOS and still holds GPU memory "
                f"(process {', '.join(str(p.pid) for p in stuck)}). Restart your computer, then retry the notes. "
                "Your transcript is saved.")

    def start(self, wait: float = 120) -> dict:
        m = self.cfg.llm.managed
        if self.proc and self.proc.poll() is None:
            return self.status()
        exe, model = self.server_binary(), self.model_path()
        if not exe or not model:
            raise RuntimeError("The managed AI model is not installed yet (Settings → AI model → Download Qwen).")
        self._kill_leftover()
        if port_open(m.port):
            raise RuntimeError(f"Port {m.port} is already in use (another AI server?). Use it as an external "
                               "profile, or change llm.managed.port.")
        gpu = str(m.gpu_layers)
        args = [str(exe), "-m", str(model), "--alias", m.alias, "--host", "127.0.0.1", "--port", str(m.port),
                "-c", str(m.context), "-ngl", gpu, "--jinja"]
        if m.context <= 16384:
            args += ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
        args += list(m.extra_args)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
        self._log_fh = open(self.root / "llama-server.log", "ab")
        flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        log.info("Starting managed LLM: %s", " ".join(args))
        # Run under a watchdog so the server never outlives Modwen (crash, closed window, Ctrl+C).
        import sys

        wrapped = [sys.executable, "-m", "modwen.runtime.babysit", str(os.getpid()), "--", *args]
        self.proc = subprocess.Popen(wrapped, stdout=self._log_fh, stderr=subprocess.STDOUT, cwd=str(exe.parent),
                                     env=env, creationflags=flags)
        self.pid_file.write_text(str(self.proc.pid))
        t0 = time.time()
        while time.time() - t0 < wait:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited (code {self.proc.returncode}); see {self.root / 'llama-server.log'}")
            try:
                if httpx.get(f"http://127.0.0.1:{m.port}/health", timeout=1).status_code == 200:
                    return self.status()
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return self.status()  # still loading; the UI keeps polling health

    @staticmethod
    def _kill_tree(pid: int) -> None:
        import psutil

        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        # SIGKILL right away. Asked to exit politely (SIGTERM), this llama.cpp build can hang forever freeing
        # Metal memory in its exit handler, leaving an unkillable process that holds GPU memory and the port
        # (seen 2026-10-20). Killed outright, the kernel frees everything; there's no state worth saving.
        procs = root.children(recursive=True) + [root]
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=6)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self._kill_tree(self.proc.pid)
        self.proc = None
        self.pid_file.unlink(missing_ok=True)
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def uninstall_model(self) -> None:
        self.stop()
        if self.models_dir.exists():
            shutil.rmtree(self.models_dir)
        self.cfg.llm.managed.model_file = ""
