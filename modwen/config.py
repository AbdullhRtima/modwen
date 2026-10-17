"""Configuration: YAML file + defaults, validated with Pydantic.

Lookup order for the config file:
  1. explicit path (``--config``)
  2. ``MODWEN_CONFIG`` environment variable
  3. ``./config.yaml`` in the current directory
  4. the OS config dir (e.g. ``~/Library/Application Support/Modwen/config.yaml``)
If none exists, defaults are used.

Secrets are never stored in plain text: values like ``env:OPENAI_API_KEY`` or ``keychain:huggingface``
are resolved at runtime by :func:`resolve_secret`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

APP_NAME = "Modwen"
LEGACY_APP_NAME = "Blue-bird"

DEFAULT_STT_PROMPT = (
    "اجتماع الفريق. ناقشنا الـ roadmap والـ sprint planning، و Ahmed هيعمل الـ deployment يوم Thursday. "
    "Meeting notes: release plan, QA, action items."
)


class AppConfig(BaseModel):
    ui_host: str = "127.0.0.1"
    ui_port: int = 8765
    hotkey: str = "CmdOrCtrl+Shift+B"
    data_dir: str = "auto"
    open_browser: bool = True
    setup_complete: bool = False  # first-run onboarding finished

    @field_validator("ui_host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("ui_host must be a loopback address (Modwen is local-only)")
        return v


class CaptureConfig(BaseModel):
    sources: list[Literal["mic", "system"]] = ["mic", "system"]
    mic_device: str | int | None = None
    # auto: Core Audio tap on macOS 14.2+, WASAPI loopback on Windows, PulseAudio/PipeWire monitor on Linux.
    # device: read a loopback input device such as "BlackHole 2ch" (set system_device).
    system_backend: Literal["auto", "coreaudio_tap", "wasapi", "pulse", "device", "none"] = "auto"
    system_device: str | int | None = None
    sample_rate: int = 16000
    chunk_seconds: int = 30
    max_hours: float = 4.0
    warn_hours: float = 3.0
    min_free_gb: float = 2.0
    silence_warning_seconds: int = 60


class STTPassConfig(BaseModel):
    provider: Literal["local_whisper", "fake", "none"] = "local_whisper"
    model: str = "auto"  # auto -> chosen by hardware tier
    backend: Literal["auto", "faster_whisper", "mlx"] = "auto"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"
    beam_size: int = 5


class STTConfig(BaseModel):
    live: STTPassConfig = STTPassConfig(beam_size=1)
    final: STTPassConfig = STTPassConfig()
    meeting_language: Literal["auto", "en", "ar"] = "auto"
    allowed_languages: list[str] = ["ar", "en"]
    arabic_threshold: float = 0.2
    initial_prompt: str = DEFAULT_STT_PROMPT
    initial_prompt_file: str | None = None
    glossary: list[str] = []
    extra_hallucination_phrases: list[str] = []
    max_segment_seconds: float = 20.0
    vad: Literal["auto", "silero", "energy"] = "auto"


class DiarizationConfig(BaseModel):
    provider: Literal["auto", "pyannote", "none"] = "auto"
    model: str = "pyannote/speaker-diarization-community-1"
    hf_token: str | None = "keychain:huggingface"
    min_speakers: int | None = None
    max_speakers: int | None = None
    voiceprint_threshold: float = 0.70


class LLMProfile(BaseModel):
    base_url: str
    api_key: str = "sk-local"
    model: str
    context_window: int = 16384
    structured_output: Literal["json_schema", "json_object", "prompt_only"] = "json_schema"
    temperature: float = 0.3
    max_output_tokens: int = 2048
    extra_body: dict[str, Any] = {}
    strip_think: bool = True
    no_think_suffix: bool = False  # append "/no_think" to the last user message (Qwen3 hybrid models via Ollama)
    timeout_seconds: float = 900


def _default_profiles() -> dict[str, LLMProfile]:
    return {
        "local_qwen": LLMProfile(
            base_url="http://localhost:8080/v1",
            model="qwen3-8b",
            context_window=16384,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        ),
        "ollama_qwen": LLMProfile(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            model="qwen3:8b",
            context_window=16384,
            no_think_suffix=True,
        ),
    }


class ManagedLLMConfig(BaseModel):
    """A llama.cpp server that Modwen downloads, starts and stops itself."""
    enabled: bool = False
    autostart: bool = True
    model_choice: str = ""  # qwen3-4b | qwen3-8b | qwen3-14b | qwen3-30b-a3b
    model_file: str = ""
    alias: str = "qwen3-8b"
    context: int = 16384
    port: int = 8080
    gpu_layers: int = 99
    variant: str = "auto"  # auto | macos | cuda | vulkan | cpu
    extra_args: list[str] = []


class LLMConfig(BaseModel):
    active: str = "local_qwen"
    fallback: list[str] = ["ollama_qwen"]
    profiles: dict[str, LLMProfile] = Field(default_factory=_default_profiles)
    managed: ManagedLLMConfig = ManagedLLMConfig()

    def profile(self, name: str | None = None) -> LLMProfile:
        key = name or self.active
        if key not in self.profiles:
            raise KeyError(f"LLM profile '{key}' is not defined in config (have: {', '.join(self.profiles)})")
        return self.profiles[key]


class NotesConfig(BaseModel):
    summary_language: Literal["en", "ar", "both"] = "en"
    keep_original_quotes: bool = True
    timezone: str = "system"
    calendar: Literal["gregorian"] = "gregorian"
    chunk_minutes: float = 15
    chunk_overlap_minutes: float = 1


class NotifyConfig(BaseModel):
    desktop: bool = True
    sound: bool = True
    on_failure: bool = True


class RetentionConfig(BaseModel):
    audio_days: int = 7
    transcript_days: int = 90
    notes_days: int = 365


class PrivacyConfig(BaseModel):
    # Fixed by policy: Modwen never sends meeting audio or text to a cloud service.
    # The value is accepted for config-file compatibility but cannot be turned on.
    allow_cloud: bool = False
    offline_models: bool = True
    telemetry: bool = False
    keystore: Literal["auto", "keyring", "file"] = "auto"
    retention: RetentionConfig = RetentionConfig()

    @field_validator("allow_cloud")
    @classmethod
    def _never_cloud(cls, v: bool) -> bool:
        if v:
            log.warning("privacy.allow_cloud=true is ignored: Modwen is local-only by policy.")
        return False


class EmailConfig(BaseModel):
    enabled: bool = False  # Phase 3
    provider: Literal["gmail_api", "smtp"] = "gmail_api"


class Config(BaseModel):
    app: AppConfig = AppConfig()
    capture: CaptureConfig = CaptureConfig()
    stt: STTConfig = STTConfig()
    diarization: DiarizationConfig = DiarizationConfig()
    llm: LLMConfig = LLMConfig()
    notes: NotesConfig = NotesConfig()
    notify: NotifyConfig = NotifyConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    email: EmailConfig = EmailConfig()

    loaded_from: str | None = Field(default=None, exclude=True)

    # ----- derived paths -----
    @property
    def data_dir(self) -> Path:
        env = os.environ.get("MODWEN_DATA_DIR")
        if env:
            p = Path(env)
        elif self.app.data_dir != "auto":
            p = Path(self.app.data_dir).expanduser()
        else:
            p = Path(user_data_dir(APP_NAME, appauthor=False))
            legacy = Path(user_data_dir(LEGACY_APP_NAME, appauthor=False))
            if not p.exists() and legacy.is_dir():
                legacy.rename(p)  # the project was called "Blue-bird" before 0.2: keep existing meetings
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def models_dir(self) -> Path:
        p = self.data_dir / "models"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        path = self.data_dir / "modwen.db"
        legacy = self.data_dir / "blue-bird.db"  # name before 0.2
        if not path.exists() and legacy.exists():
            import sqlite3

            con = sqlite3.connect(legacy)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold pending journal pages into the main file
            con.close()
            legacy.rename(path)
            for suffix in ("-wal", "-shm"):
                leftover = legacy.with_name(legacy.name + suffix)
                if leftover.exists():
                    leftover.unlink()
        return path

    @property
    def audio_dir(self) -> Path:
        p = self.data_dir / "audio"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def stt_prompt(self) -> str:
        prompt = self.stt.initial_prompt
        if self.stt.initial_prompt_file:
            path = Path(self.stt.initial_prompt_file).expanduser()
            if path.exists():
                prompt = path.read_text(encoding="utf-8").strip()
        if self.stt.glossary:
            prompt = f"{prompt} {', '.join(self.stt.glossary)}."
        return prompt


def default_config_path() -> Path:
    path = Path(user_config_dir(APP_NAME, appauthor=False)) / "config.yaml"
    legacy = Path(user_config_dir(LEGACY_APP_NAME, appauthor=False)) / "config.yaml"
    return legacy if not path.exists() and legacy.exists() else path


def find_config_file(explicit: str | os.PathLike | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("MODWEN_CONFIG"):
        candidates.append(Path(os.environ["MODWEN_CONFIG"]))
    candidates.append(Path.cwd() / "config.yaml")
    candidates.append(default_config_path())
    for c in candidates:
        if c.expanduser().is_file():
            return c.expanduser()
    if explicit:
        raise FileNotFoundError(f"Config file not found: {explicit}")
    return None


def load_config(path: str | os.PathLike | None = None) -> Config:
    found = find_config_file(path)
    data: dict[str, Any] = {}
    if found:
        with open(found, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        # Profiles in the file replace the defaults entirely only if the user defines 'profiles'.
    cfg = Config.model_validate(data)
    cfg.loaded_from = str(found) if found else None
    return cfg


def save_config(cfg: Config, path: str | os.PathLike | None = None) -> Path:
    target = (Path(path) if path else Path(cfg.loaded_from) if cfg.loaded_from
              else Path(os.environ["MODWEN_CONFIG"]) if os.environ.get("MODWEN_CONFIG") else default_config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude={"loaded_from"})
    with open(target, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    cfg.loaded_from = str(target)
    return target


def resolve_secret(value: str | None, cfg: Config | None = None) -> str | None:
    """Resolve ``env:NAME`` and ``keychain:NAME`` references. Plain strings are returned unchanged."""
    if value is None:
        return None
    if value.startswith("env:"):
        return os.environ.get(value[4:])
    if value.startswith("keychain:"):
        name = value[len("keychain:") :]
        from modwen.crypto import SecretStore

        return SecretStore(cfg).get(name)
    return value
