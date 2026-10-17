"""Shared fixtures: an isolated data dir inside the project, file-based keystore, fake LLM server."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

import httpx
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = ROOT / ".tmp" / "tests"


@pytest.fixture()
def data_dir(monkeypatch):
    d = TMP_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MODWEN_DATA_DIR", str(d))
    monkeypatch.setenv("MODWEN_KEYSTORE", "file")  # never touch the real OS keychain in tests
    monkeypatch.setenv("MODWEN_CONFIG", str(d / "config.yaml"))  # saves never leave the test folder
    monkeypatch.chdir(d)  # so ./config.yaml from the repo is not picked up
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def cfg(data_dir):
    from modwen.config import Config

    c = Config()
    c.loaded_from = str(data_dir / "config.yaml")
    c.notify.desktop = False
    c.stt.live.provider = "none"
    c.stt.vad = "energy"  # synthetic tones stand in for speech; Silero (correctly) ignores tones
    return c


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def speechy(pattern: list[tuple[float, float]]) -> np.ndarray:
    """Concatenate (tone_seconds, silence_seconds) blocks; tones stand in for speech for the energy VAD."""
    parts = []
    for on, off in pattern:
        parts.append(tone(on))
        parts.append(np.zeros(int(off * 16000), dtype=np.float32))
    return np.concatenate(parts)


class FakeLLM:
    """An OpenAI-compatible /v1 server implemented as an httpx MockTransport."""

    def __init__(self):
        self.requests: list[dict] = []
        self.extract_replies: list[str] = []  # optional scripted replies for extract calls
        self.summary_replies: dict[str, list[str]] = {"en": [], "ar": []}

    def _ids(self, text: str) -> list[str]:
        return re.findall(r"#(s\d{4})", text)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen3-8b", "object": "model"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        user = body["messages"][-1]["content"]
        if "Merge them into one list" in user:
            content = json.dumps({"key_points": [{"text": "Merged point", "original_text": "",
                                                  "evidence_segment_ids": ["s0001"]}],
                                  "decisions": [], "action_items": [], "open_questions": []})
        elif "اكتب ملخص" in user:
            content = self.summary_replies["ar"].pop(0) if self.summary_replies["ar"] else json.dumps(
                {"summary": "اتفق الفريق على إطلاق الإصدار يوم الخميس، وسيُنهي أحمد اختبارات الجودة قبل ذلك."})
        elif "Write the summary" in user:
            content = self.summary_replies["en"].pop(0) if self.summary_replies["en"] else json.dumps(
                {"summary": "The team agreed to release on Thursday. Ahmed will finish QA before the release."})
        else:
            if self.extract_replies:
                content = self.extract_replies.pop(0)
            else:
                ids = self._ids(user) or ["s0001"]
                content = json.dumps({
                    "key_points": [{"text": "Release checklist reviewed", "original_text": "",
                                    "evidence_segment_ids": [ids[0]]}],
                    "decisions": [{"text": "Release on Thursday", "original_text": "الـ release يوم الخميس",
                                   "evidence_segment_ids": [ids[-1]]}],
                    "action_items": [{"task": "Finish QA before release", "task_original": "هخلّص الـ QA",
                                      "owner": "Ahmed", "due_raw": "الخميس", "due_date": None,
                                      "evidence_segment_ids": [ids[-1]]},
                                     {"task": "Run regression tests", "task_original": "", "owner": None,
                                      "due_raw": None, "due_date": None, "evidence_segment_ids": [ids[0]]}],
                    "open_questions": [{"question": "Who updates the client?", "question_original": "",
                                        "evidence_segment_ids": [ids[0]]}],
                })
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body.get("model", "qwen3-8b"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def fake_llm():
    return FakeLLM()
