"""One LLM client for every provider: the OpenAI Chat Completions API against a LOCAL base_url.

Switching between llama.cpp, Ollama, LM Studio or vLLM is a config change (``llm.active``).
Non-loopback URLs are refused by the egress gate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

import httpx
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from modwen.config import LLMProfile, resolve_secret
from modwen.egress import ensure_local

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The reply hit max_tokens. Retrying the same prompt won't help; ask for less instead."""


def strip_think(text: str) -> str:
    text = THINK_RE.sub("", text)
    # An unterminated <think> (truncated output): drop everything before the first JSON brace.
    if "<think>" in text.lower():
        idx = text.find("{")
        text = text[idx:] if idx >= 0 else ""
    return text.strip()


def extract_json(text: str) -> dict:
    text = strip_think(text)
    m = FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(self, profile: LLMProfile, name: str = "custom", cfg=None, http_client: httpx.Client | None = None):
        ensure_local(profile.base_url, "LLM base_url")
        self.profile = profile
        self.name = name
        self.mode = profile.structured_output
        self.client = OpenAI(
            base_url=profile.base_url,
            api_key=resolve_secret(profile.api_key, cfg) or "sk-local",
            timeout=profile.timeout_seconds,
            max_retries=1,
            http_client=http_client,
        )

    # ------------------------------------------------------------------
    def health(self) -> dict:
        try:
            models = [m.id for m in self.client.models.list().data]
            return {"ok": True, "models": models, "base_url": self.profile.base_url, "model": self.profile.model}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "base_url": self.profile.base_url, "model": self.profile.model}

    def chat(self, messages: list[dict], *, schema: type[BaseModel] | None = None, max_tokens: int | None = None,
             temperature: float | None = None) -> str:
        msgs = [dict(m) for m in messages]
        if self.profile.no_think_suffix and msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = msgs[-1]["content"].rstrip() + " /no_think"
        kwargs: dict = {
            "model": self.profile.model,
            "messages": msgs,
            "temperature": self.profile.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.profile.max_output_tokens,
        }
        if self.profile.extra_body:
            kwargs["extra_body"] = self.profile.extra_body
        if schema is not None and self.mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
            }
        elif schema is not None and self.mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            # Some servers reject json_schema; degrade once to json_object, then to prompt-only.
            if schema is not None and self.mode != "prompt_only":
                log.warning("Server rejected response_format=%s (%s); degrading", self.mode, exc)
                self.mode = "json_object" if self.mode == "json_schema" else "prompt_only"
                return self.chat(messages, schema=schema, max_tokens=max_tokens, temperature=temperature)
            raise LLMError(str(exc)) from exc
        except Exception as exc:
            raise LLMError(f"LLM request to {self.profile.base_url} failed: {exc}") from exc
        choice = resp.choices[0]
        content = choice.message.content or ""
        if choice.finish_reason == "length":
            raise LLMTruncated(f"LLM reply was cut off at max_tokens={kwargs['max_tokens']} "
                               f"({len(content)} chars); the input needs to be smaller")
        return strip_think(content) if self.profile.strip_think else content

    def complete_json(self, messages: list[dict], model: type[T], *, retries: int = 2,
                      max_tokens: int | None = None, validate=None) -> T:
        """Ask for JSON matching ``model``; on invalid output feed the error back and retry.

        A truncated reply (``LLMTruncated``) is raised at once: the same prompt would be cut off again.
        """
        msgs = list(messages)
        last_err = ""
        for attempt in range(retries + 1):
            text = self.chat(msgs, schema=model, max_tokens=max_tokens)
            try:
                obj = model.model_validate(extract_json(text))
                if validate:
                    problem = validate(obj)
                    if problem:
                        raise ValueError(problem)
                return obj
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_err = str(exc)[:500]
                log.info("LLM reply rejected (attempt %d): %s", attempt + 1, last_err)
                msgs = list(messages) + [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": f"That reply was not acceptable: {last_err}\n"
                                                f"Reply again with ONLY the corrected JSON object."},
                ]
        raise LLMError(f"LLM did not return valid JSON after {retries + 1} attempts: {last_err}")


def make_client(cfg, profile_name: str | None = None, http_client: httpx.Client | None = None) -> LLMClient:
    name = profile_name or cfg.llm.active
    return LLMClient(cfg.llm.profile(name), name=name, cfg=cfg, http_client=http_client)
