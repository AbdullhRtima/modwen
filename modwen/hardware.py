"""Hardware detection and model selection by tier (design §6.1)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

import psutil


@dataclass
class HardwareInfo:
    os: str
    os_version: str
    arch: str
    cpu: str
    cores: int
    ram_gb: float
    apple_silicon: bool
    gpu_vendor: str | None = None  # "nvidia" | "apple" | None
    gpu_name: str | None = None
    vram_gb: float | None = None
    free_disk_gb: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TierPlan:
    tier: str
    label: str
    whisper_backend: str  # "faster_whisper" | "mlx"
    device: str  # "cuda" | "cpu" | "metal"
    compute_type: str
    live_model: str
    final_model: str
    llm_model: str
    llm_hf: str
    llm_alias: str
    llm_context: int
    est_2h_minutes: tuple[int, int]
    warnings: list[str] = field(default_factory=list)

    def llama_server_command(self, port: int = 8080) -> str:
        gpu = "-ngl 99" if self.device in ("cuda", "metal") else "-ngl 0"
        kv = " --cache-type-k q8_0 --cache-type-v q8_0" if self.llm_context <= 16384 else ""
        return (
            f"llama-server -hf {self.llm_hf} --alias {self.llm_alias} "
            f"--host 127.0.0.1 --port {port} -c {self.llm_context} {gpu} --jinja{kv}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["llama_server_command"] = self.llama_server_command()
        return d


def _cpu_name() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            return subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=3
            ).stdout.strip()
        if system == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or platform.machine()


def _nvidia() -> tuple[str, float] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not out:
            return None
        name, mem = out.splitlines()[0].rsplit(",", 1)
        return name.strip(), round(float(mem) / 1024, 1)
    except Exception:
        return None


def detect(data_dir: str | os.PathLike | None = None) -> HardwareInfo:
    system = platform.system()
    arch = platform.machine().lower()
    apple_silicon = system == "Darwin" and arch in ("arm64", "aarch64")
    ram_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    info = HardwareInfo(
        os={"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(system, system),
        os_version=platform.mac_ver()[0] if system == "Darwin" else platform.version(),
        arch=arch,
        cpu=_cpu_name(),
        cores=psutil.cpu_count(logical=False) or os.cpu_count() or 1,
        ram_gb=ram_gb,
        apple_silicon=apple_silicon,
    )
    if apple_silicon:
        info.gpu_vendor, info.gpu_name, info.vram_gb = "apple", info.cpu, ram_gb  # unified memory
    else:
        nv = _nvidia()
        if nv:
            info.gpu_vendor, info.gpu_name, info.vram_gb = "nvidia", nv[0], nv[1]
    try:
        info.free_disk_gb = round(shutil.disk_usage(data_dir or os.getcwd()).free / 1024**3, 1)
    except Exception:
        pass
    return info


def _apple(cpu: str) -> str:
    return cpu if cpu.lower().startswith("apple") else f"Apple {cpu}"


def choose_tier(info: HardwareInfo) -> TierPlan:
    """Map detected hardware to the tier table in docs/system-design.md §6.1."""
    q8 = ("Qwen3-8B", "Qwen/Qwen3-8B-GGUF:Q4_K_M", "qwen3-8b")
    q14 = ("Qwen3-14B", "Qwen/Qwen3-14B-GGUF:Q4_K_M", "qwen3-14b")
    q30 = ("Qwen3-30B-A3B-Instruct-2507", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q4_K_M", "qwen3-30b-a3b")
    q4 = ("Qwen3-4B-Instruct-2507", "unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M", "qwen3-4b")
    warnings: list[str] = []

    if info.ram_gb < 15:
        warnings.append(
            f"Only {info.ram_gb} GB RAM: below the 16 GB minimum. 2-hour meetings are not supported."
        )

    if info.gpu_vendor == "nvidia" and (info.vram_gb or 0) >= 11.5:
        return TierPlan("A", f"NVIDIA {info.gpu_name}, {info.vram_gb} GB VRAM", "faster_whisper", "cuda",
                        "float16", "large-v3-turbo", "large-v3", *q14, 32768, (10, 18), warnings)
    if info.gpu_vendor == "nvidia" and (info.vram_gb or 0) >= 7.5:
        return TierPlan("B", f"NVIDIA {info.gpu_name}, {info.vram_gb} GB VRAM", "faster_whisper", "cuda",
                        "int8_float16", "large-v3-turbo", "large-v3", *q8, 16384, (15, 25),
                        warnings + ["8 GB VRAM: Modwen runs Whisper and the LLM one after another."])
    if info.apple_silicon and info.ram_gb >= 31:
        return TierPlan("C", f"{_apple(info.cpu)}, {info.ram_gb:.0f} GB unified memory", "mlx", "metal",
                        "float16", "large-v3-turbo", "large-v3", *q30, 32768, (25, 40), warnings)
    if info.apple_silicon:
        return TierPlan("D", f"{_apple(info.cpu)}, {info.ram_gb:.0f} GB unified memory", "mlx", "metal",
                        "float16", "large-v3-turbo", "large-v3", *q8, 16384, (30, 45), warnings)
    if info.ram_gb >= 31:
        return TierPlan("E", f"CPU only ({info.cpu}), {info.ram_gb:.0f} GB RAM", "faster_whisper", "cpu",
                        "int8", "small", "large-v3-turbo", *q30, 32768, (45, 90), warnings)
    return TierPlan("F", f"CPU only ({info.cpu}), {info.ram_gb:.0f} GB RAM", "faster_whisper", "cpu", "int8",
                    "base", "large-v3-turbo", *q4, 16384, (60, 120),
                    warnings + ["Tier F: Arabic summaries will be weak; keep the English default."])


def mlx_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401

        return True
    except Exception:
        return False


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
