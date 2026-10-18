"""Notes engine against a fake OpenAI-compatible server: single pass, map-reduce, retries, validation."""

from __future__ import annotations

import json

import pytest


def _segments(n: int, step_ms: int = 10_000, text: str = "We should release on Thursday after QA."):
    return [{"id": f"s{i:04d}", "start_ms": i * step_ms, "end_ms": i * step_ms + 8000, "text": text,
             "language": "en", "speaker_name": "Ahmed" if i % 2 else "You"} for i in range(1, n + 1)]


def _engine(cfg, fake_llm, **profile_overrides):
    from modwen.notes.engine import NotesEngine
    from modwen.notes.llm_client import make_client

    for k, v in profile_overrides.items():
        setattr(cfg.llm.profiles[cfg.llm.active], k, v)
    return NotesEngine(make_client(cfg, http_client=fake_llm.client()), cfg.notes)


def test_single_pass(cfg, fake_llm):
    eng = _engine(cfg, fake_llm)
    res = eng.generate(title="Sprint review", started_at="2026-10-16T10:00:00+03:00", segments=_segments(5))
    d = res.data
    assert d["summary"]["en"].startswith("The team agreed")
    assert "ar" not in d["summary"]
    assert d["parts"] == 1
    assert d["decisions"][0]["evidence_segment_ids"] == ["s0005"]
    a = d["action_items"][0]
    # The model said "الخميس" but the cited line is English ("...on Thursday...") -> re-read from the transcript
    assert a["owner"] == "Ahmed" and a["due_raw"] == "thursday" and a["due_date"] == "2026-10-22"
    # No owner named by the model: the only speaker on the cited line (s0001, Ahmed) becomes the owner
    assert d["action_items"][1]["owner"] == "Ahmed"
    assert not any("without an owner" in w for w in d["warnings"])
    # Structured output requested from the server, thinking disabled for Qwen via extra_body
    req = fake_llm.requests[0]
    assert req["response_format"]["type"] == "json_schema"
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert res.prompt_version.startswith("notes-v")


def test_both_languages_and_cjk_retry(cfg, fake_llm):
    fake_llm.summary_replies["ar"] = [json.dumps({"summary": "اتفق الفريق 你好 على الإطلاق يوم الخميس وأحمد مسؤول."})]
    eng = _engine(cfg, fake_llm)
    res = eng.generate(title="t", started_at=None, segments=_segments(3), summary_language="both")
    assert set(res.data["summary"]) == {"en", "ar"}
    assert "你" not in res.data["summary"]["ar"]
    assert any("Chinese" in m["content"] for r in fake_llm.requests for m in r["messages"] if m["role"] == "user")


def test_invalid_json_then_valid(cfg, fake_llm):
    fake_llm.extract_replies = ["<think>hmm</think> sorry, here you go: not json",
                                '```json\n{"key_points": [], "decisions": [], "action_items": [], '
                                '"open_questions": [{"question": "Q?", "evidence_segment_ids": ["#s0001"]}]}\n```']
    eng = _engine(cfg, fake_llm)
    res = eng.generate(title="t", started_at=None, segments=_segments(2))
    assert res.data["open_questions"][0]["evidence_segment_ids"] == ["s0001"]


def test_bogus_evidence_is_rejected_then_low_confidence(cfg, fake_llm):
    bogus = json.dumps({"key_points": [{"text": "x", "evidence_segment_ids": ["zz99"]}],
                        "decisions": [], "action_items": [], "open_questions": []})
    fake_llm.extract_replies = [bogus, bogus, bogus]
    eng = _engine(cfg, fake_llm)
    from modwen.notes.llm_client import LLMError

    with pytest.raises(LLMError):
        eng.generate(title="t", started_at=None, segments=_segments(2))


def test_truncated_extract_splits_window(cfg, fake_llm):
    # A dense window whose items overflow max_output_tokens: split in half, don't retry the same prompt.
    fake_llm.truncate_extract_over = 3
    eng = _engine(cfg, fake_llm)
    res = eng.generate(title="t", started_at=None, segments=_segments(8))
    sizes = [len(fake_llm._ids(r["messages"][-1]["content"])) for r in fake_llm.requests
             if "#s0" in r["messages"][-1]["content"]]
    assert sizes == [8, 4, 2, 2, 4, 2, 2]  # each cut-off window is tried once, then halved
    assert res.data["parts"] == 1
    assert len(res.data["decisions"]) == 4  # one per successful half, all kept


def test_truncated_single_segment_raises(cfg, fake_llm):
    from modwen.notes.llm_client import LLMTruncated

    fake_llm.truncate_extract_over = 0
    eng = _engine(cfg, fake_llm)
    with pytest.raises(LLMTruncated):
        eng.generate(title="t", started_at=None, segments=_segments(1))
    assert len(fake_llm.requests) == 1  # no pointless retries


def test_map_reduce_for_long_meetings(cfg, fake_llm):
    # 2 hours of dense speech, small context window -> several windows + a reduce call
    segs = _segments(720, step_ms=10_000, text="نراجع الـ release plan ونقرر مين يعمل الـ QA قبل يوم الخميس " * 2)
    eng = _engine(cfg, fake_llm, context_window=8192)
    parts = []
    res = eng.generate(title="Long", started_at=None, segments=segs, progress=lambda d, t: parts.append((d, t)))
    assert res.data["parts"] >= 8
    assert any("Merge them into one list" in r["messages"][-1]["content"] for r in fake_llm.requests)
    assert parts[-1][0] == parts[-1][1]  # progress reached the end
    assert res.data["key_points"][0]["text"] == "Merged point"


def test_no_think_suffix_for_ollama(cfg, fake_llm):
    cfg.llm.active = "ollama_qwen"
    eng = _engine(cfg, fake_llm)
    eng.generate(title="t", started_at=None, segments=_segments(2))
    assert fake_llm.requests[0]["messages"][-1]["content"].endswith("/no_think")


def test_degrades_when_json_schema_rejected(cfg):
    import httpx

    from modwen.notes.llm_client import make_client
    from modwen.notes.schema import SummaryResult

    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body.get("response_format", {}).get("type"))
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                             "role": "assistant", "content": '{"summary": "ok fine summary text"}'}}]})

    client = make_client(cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.complete_json([{"role": "user", "content": "hi"}], SummaryResult)
    assert out.summary.startswith("ok")
    assert seen[:2] == ["json_schema", "json_object"]


def test_markdown_render(cfg, fake_llm):
    from modwen.notes.render import notes_markdown

    eng = _engine(cfg, fake_llm)
    segs = _segments(3)
    res = eng.generate(title="Sprint review", started_at="2026-10-16T10:00:00", segments=segs)
    md = notes_markdown({"title": "Sprint review", "started_at": "2026-10-16T10:00:00", "duration_ms": 7_500_000},
                        {"data": res.data}, segs)
    assert md.startswith("# Meeting notes: Sprint review")
    assert "2h 05m" in md and "| Ahmed | Finish QA before release | 2026-10-22 |" in md
    only = notes_markdown({"title": "x", "started_at": "2026-10-16"}, {"data": res.data}, segs, actions_only=True)
    assert "Action items" in only and "Summary" not in only


def test_hallucinated_deadline_is_replaced_or_dropped(cfg, fake_llm):
    segs = [{"id": "s0001", "start_ms": 0, "end_ms": 5000, "language": "en", "speaker_name": "Others",
             "text": "Sarah will send the security sign-off request tomorrow."},
            {"id": "s0002", "start_ms": 6000, "end_ms": 9000, "language": "en", "speaker_name": "Others",
             "text": "Omar will clean up the backlog."}]
    fake_llm.extract_replies = [json.dumps({"key_points": [], "decisions": [], "open_questions": [], "action_items": [
        {"task": "Send sign-off request", "owner": "Sarah", "due_raw": "next Monday", "due_date": "2026-10-19",
         "evidence_segment_ids": ["s0001"]},
        {"task": "Clean up backlog", "owner": "Omar", "due_raw": "Friday", "due_date": "2026-10-23",
         "evidence_segment_ids": ["s0002"]}]})]
    eng = _engine(cfg, fake_llm)
    d = eng.generate(title="t", started_at="2026-10-16T10:00:00", segments=segs).data
    a1, a2 = d["action_items"]
    assert (a1["due_raw"], a1["due_date"]) == ("tomorrow", "2026-10-17")
    assert (a2["due_raw"], a2["due_date"]) == (None, None)
    assert any("Dropped a deadline" in w for w in d["warnings"])
