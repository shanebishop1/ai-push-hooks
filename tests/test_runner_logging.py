from __future__ import annotations

import io
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_push_hooks.types import HookLogger


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_console_color_policy_respects_tty_force_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _TTY()
    monkeypatch.setattr("ai_push_hooks.types.sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)

    HookLogger(None, console_level="info").info("test", "info")
    assert "\x1b[36m[ai-push-hooks]\x1b[0m" in stream.getvalue()

    stream.seek(0)
    stream.truncate(0)
    monkeypatch.setenv("NO_COLOR", "")
    HookLogger(None, console_level="info").info("test", "info")
    assert "\x1b[" not in stream.getvalue()

    stream.seek(0)
    stream.truncate(0)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "0")
    HookLogger(None).warn("test", "warning")
    assert "\x1b[" not in stream.getvalue()

    stream.seek(0)
    stream.truncate(0)
    monkeypatch.setenv("FORCE_COLOR", "1")
    HookLogger(None).error("test", "error")
    assert "\x1b[31m[ai-push-hooks]\x1b[0m" in stream.getvalue()


def test_non_tty_and_dumb_terminal_are_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("ai_push_hooks.types.sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")

    HookLogger(None).status("test", "plain")

    assert stream.getvalue() == "[ai-push-hooks] plain\n"


def test_jsonl_is_structured_plain_and_sanitized(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("ai_push_hooks.types.sys.stderr", stream)
    monkeypatch.setenv("FORCE_COLOR", "1")
    path = tmp_path / "events.jsonl"
    logger = HookLogger(path, console_level="info")

    logger.info("event", "hello\x1b[31m\nforged", value="safe\x1b[2J")

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["event"] == "event"
    assert record["message"] == "hello\nforged"
    assert record["value"] == "safe"
    assert "\x1b[" not in path.read_text(encoding="utf-8")
    assert "\x1b[36m[ai-push-hooks]\x1b[0m" in stream.getvalue()


def test_llm_console_uses_semantic_accents(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _TTY()
    monkeypatch.setattr("ai_push_hooks.types.sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    logger = HookLogger(None)
    call_number = logger.llm_call(
        "docs.query",
        "llm:query",
        "model",
        runner_profile="review",
        runner_type="command",
    )
    logger.llm_complete(
        call_number,
        "docs.query",
        "review",
        "command",
        session_id="session-1",
        session_state="persisted",
        resumable=True,
        transcript="transcript.json",
        resume_command="runner --resume session-1",
    )
    logger.llm_complete(2, "docs.query", "review", "command", failed=True)

    output = stream.getvalue()
    assert "\x1b[36mdocs\x1b[0m\x1b[2m.\x1b[0m\x1b[35mquery\x1b[0m" in output
    assert "\x1b[1m#1\x1b[0m" in output
    assert "\x1b[34mllm:query\x1b[0m" in output
    assert "\x1b[32mLLM complete\x1b[0m" in output
    assert "\x1b[31mLLM failed\x1b[0m" in output
    assert "\x1b[2m; session persisted: session-1; resume: runner --resume session-1; transcript: transcript.json\x1b[0m" in output


def test_llm_completion_reports_each_session_lifecycle(capsys: pytest.CaptureFixture[str]) -> None:
    logger = HookLogger(None)

    logger.llm_complete(1, "docs.query", "review", "command")
    logger.llm_complete(
        2,
        "docs.query",
        "review",
        "opencode",
        session_id="session-persisted",
        session_state="persisted",
        resumable=True,
        resume_command="opencode --session session-persisted",
        transcript="persisted-transcript.json",
    )
    logger.llm_complete(
        3,
        "docs.apply",
        "writer",
        "opencode",
        session_id="session-deleted",
        session_state="deleted",
        transcript=".git/transcripts/docs.json",
    )
    logger.llm_complete(
        4,
        "docs.query",
        "review",
        "claude",
        session_id="session-ephemeral",
        session_state="ephemeral",
    )
    logger.llm_complete(5, "docs.query", "review", "command", failed=True)
    logger.llm_complete(
        6,
        "docs.query",
        "review",
        "command",
        session_id="session-no-resume",
        session_state="persisted",
        resume_command="must-not-be-emitted",
    )

    output = capsys.readouterr().err
    assert "LLM complete #1: docs.query (review/command)" in output
    assert "session-persisted" not in output.split("LLM complete #1: docs.query (review/command)", 1)[1].splitlines()[0]
    assert "resume: opencode --session session-persisted" in output
    assert "transcript: persisted-transcript.json" in output
    assert "session deleted: session-deleted; transcript: .git/transcripts/docs.json" in output
    assert "session: session-ephemeral; not resumable" in output
    assert "LLM failed #5: docs.query (review/command)" in output
    assert "must-not-be-emitted" not in output


def test_nonresumable_resume_command_is_omitted_from_jsonl(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = HookLogger(path)

    logger.llm_complete(
        1,
        "docs.query",
        "review",
        "command",
        session_id="session-1",
        session_state="persisted",
        resumable=False,
        resume_command="must-not-be-emitted",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert "resume_command" not in record
    assert "must-not-be-emitted" not in record["message"]


def test_llm_calls_are_numbered_and_completion_associated_under_concurrency(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = HookLogger(path)

    def invoke(index: int) -> tuple[int, str]:
        stage = f"docs.query{index}"
        call_number = logger.llm_call(
            stage,
            "llm:query",
            "model",
            runner_profile="review",
            runner_type="command",
        )
        logger.llm_complete(call_number, stage, "review", "command", failed=index % 2 == 0)
        return call_number, stage

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(invoke, range(40)))

    assert sorted(call_number for call_number, _ in results) == list(range(1, 41))
    assert len(logger.llm_calls) == 40
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    calls = {record["call_number"] for record in records if record["event"] == "llm.call"}
    completions = {
        record["call_number"]: record["stage_name"]
        for record in records
        if record["event"] == "llm.complete"
    }
    assert calls == set(range(1, 41))
    assert len(completions) == 40
    assert all(
        stage == next(stage for number, stage in results if number == call_number)
        for call_number, stage in completions.items()
    )
