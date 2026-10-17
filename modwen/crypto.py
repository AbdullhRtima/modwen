"""Encryption at rest.

* Every meeting gets its own random 256-bit data key; audio chunks are sealed with AES-256-GCM.
* Data keys are wrapped with a master key. The master key lives in the OS keychain (macOS Keychain,
  Windows Credential Manager, libsecret) via ``keyring``; if no keychain is usable (or
  ``privacy.keystore: file``), it falls back to a 0600 file inside the data directory.
* Deleting a meeting deletes its wrapped key, which makes its audio unrecoverable (crypto-shredding).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from modwen.config import Config

log = logging.getLogger(__name__)

SERVICE = "modwen"
# On-disk format constant (AES-GCM associated data for wrapped meeting keys). Set in 0.1 under the old project
# name; it must NEVER change, or every existing recording becomes undecryptable.
DATA_KEY_AAD = b"bluebird-data-key"
LEGACY_SERVICE = "blue-bird"  # name before 0.2
NONCE_LEN = 12


def generate_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt(key: bytes, data: bytes, aad: bytes | None = None) -> bytes:
    nonce = secrets.token_bytes(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    return AESGCM(key).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], aad)


def _keystore_mode(cfg: Config | None) -> str:
    env = os.environ.get("MODWEN_KEYSTORE")
    if env in ("file", "keyring"):
        return env
    return cfg.privacy.keystore if cfg else "auto"


class SecretStore:
    """Small secret store: OS keychain first, data-dir file as fallback."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg
        self.mode = _keystore_mode(cfg)

    def _file(self) -> Path:
        if self.cfg is None:
            from modwen.config import load_config

            self.cfg = load_config()
        return self.cfg.data_dir / ".secrets.json"

    def _read_file(self) -> dict:
        p = self._file()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def _write_file(self, data: dict) -> None:
        p = self._file()
        p.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        if self.mode in ("auto", "keyring"):
            try:
                import keyring

                val = keyring.get_password(SERVICE, name)
                if val is None:
                    old = keyring.get_password(LEGACY_SERVICE, name)
                    if old is not None:  # migrate from the old service name
                        keyring.set_password(SERVICE, name, old)
                        val = old
                if val is not None or self.mode == "keyring":
                    return val
            except Exception as exc:  # no usable backend
                if self.mode == "keyring":
                    raise
                log.debug("keyring unavailable (%s); using file store", exc)
        return self._read_file().get(name)

    def set(self, name: str, value: str) -> None:
        if self.mode in ("auto", "keyring"):
            try:
                import keyring

                keyring.set_password(SERVICE, name, value)
                return
            except Exception as exc:
                if self.mode == "keyring":
                    raise
                log.warning("keyring unavailable (%s); storing secret in data-dir file", exc)
        data = self._read_file()
        data[name] = value
        self._write_file(data)

    def delete(self, name: str) -> None:
        if self.mode in ("auto", "keyring"):
            try:
                import keyring

                keyring.delete_password(SERVICE, name)
            except Exception:
                pass
        data = self._read_file()
        if data.pop(name, None) is not None:
            self._write_file(data)


class KeyStore:
    """Holds the master key and wraps/unwraps per-meeting data keys."""

    MASTER = "master-key"

    def __init__(self, cfg: Config | None = None):
        self.secrets = SecretStore(cfg)
        self._master: bytes | None = None

    @property
    def master(self) -> bytes:
        if self._master is None:
            existing = self.secrets.get(self.MASTER)
            if existing:
                self._master = base64.b64decode(existing)
            else:
                self._master = generate_key()
                self.secrets.set(self.MASTER, base64.b64encode(self._master).decode())
        return self._master

    def new_data_key(self) -> tuple[bytes, bytes]:
        """Return (data_key, wrapped_data_key)."""
        key = generate_key()
        return key, encrypt(self.master, key, DATA_KEY_AAD)

    def unwrap(self, wrapped: bytes) -> bytes:
        return decrypt(self.master, wrapped, DATA_KEY_AAD)
