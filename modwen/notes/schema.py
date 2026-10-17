"""Schemas for what the LLM returns (lenient) and what Modwen stores (strict)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _clean_ids(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v:
        x = str(x).strip().lstrip("#")
        if x:
            out.append(x)
    return out


class LLMPoint(BaseModel):
    text: str
    original_text: str = ""
    evidence_segment_ids: list[str] = Field(default_factory=list)

    _ids = field_validator("evidence_segment_ids", mode="before")(_clean_ids)


class LLMAction(BaseModel):
    task: str
    task_original: str = ""
    owner: str | None = None
    due_raw: str | None = None
    due_date: str | None = None
    evidence_segment_ids: list[str] = Field(default_factory=list)

    _ids = field_validator("evidence_segment_ids", mode="before")(_clean_ids)


class LLMQuestion(BaseModel):
    question: str
    question_original: str = ""
    evidence_segment_ids: list[str] = Field(default_factory=list)

    _ids = field_validator("evidence_segment_ids", mode="before")(_clean_ids)


class ExtractResult(BaseModel):
    key_points: list[LLMPoint] = Field(default_factory=list)
    decisions: list[LLMPoint] = Field(default_factory=list)
    action_items: list[LLMAction] = Field(default_factory=list)
    open_questions: list[LLMQuestion] = Field(default_factory=list)


class SummaryResult(BaseModel):
    summary: str


def json_schema_for(model: type[BaseModel]) -> dict:
    """A compact JSON schema suitable for llama.cpp / Ollama / vLLM ``response_format``."""
    return model.model_json_schema()
