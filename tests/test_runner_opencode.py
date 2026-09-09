from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from ai_push_hooks.executors.runners import (
    ProcessResult,
    RunnerRequest,
    RunnerArtifact,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerResult,
    SessionMetadata,
)
from ai_push_hooks.executors.runners.opencode import OpenCodeRunner, create_runner
from ai_push_hooks.config import load_config

from .conftest import build_context, init_repo


def _request(context, *, project_access: str = "artifacts", **overrides: object) -> RunnerRequest:
    artifact = context.run_dir / "input.txt"
    artifact.write_text("artifact body\n", encoding="utf-8")
    values: dict[str, object] = {
        "profile_id": "opencode",
        "runner_type": "opencode",
        "stage": "docs.query",
        "purpose": "llm:query",
        "mode": "llm",
        "instruction": "Return a JSON array.",
        "artifacts": (RunnerArtifact("input.txt", "artifact body\n", artifact),),
        "cwd": context.repo_root,
        "timeout_seconds": 3,
        "model": "profile/model",
        "variant": "profile-variant",
        "project_access": project_access,
        "integration_context": context,
    }
    values.update(overrides)
    return RunnerRequest(**values)


def test_create_runner_exposes_resume_finalize_and_transcript_capabilities() -> None:
    runner = create_runner()

    assert isinstance(runner, OpenCodeRunner)
    assert runner.capabilities.supports_resume
    assert runner.capabilities.supports_finalize
    assert runner.capabilities.supports_transcript


def test_project_request_uses_effective_model_variant_and_rooted_read_policy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return ProcessResult(
            returncode=0,
            stdout='{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            stderr="",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    result = OpenCodeRunner().run(_request(context, project_access="project"))

    assert result.final_text == "[]"
    assert context.logger.llm_calls == []
    argv = captured["argv"]
    assert argv[0:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        "ai-push-hooks-readonly",
        "--pure",
        "--format",
        "json",
    ]
    assert ["--model", "profile/model"] == argv[7:9]
    assert "--variant" in argv and "profile-variant" in argv
    assert captured["cwd"] == repo.resolve()
    import json

    security = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    permissions = security["agent"]["ai-push-hooks-readonly"]["permission"]
    assert permissions["edit"] == "deny"
    for tool in ("read", "list", "glob", "grep"):
        assert permissions[tool] == "allow"
    for tool in ("bash", "task", "webfetch", "websearch", "skill", "todowrite", "question"):
        assert permissions[tool] == "deny"
    assert security["plugin"] == []
    assert security["mcp"] == {}


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ('not json\n', RunnerProtocolError),
        ('{"type":"step_start"}\n', RunnerMissingOutputError),
    ],
)
def test_malformed_or_incomplete_output_fails_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: type[Exception],
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, stdout, ""),
    )

    with pytest.raises(expected):
        OpenCodeRunner().run(_request(context))


def test_truncated_process_output_fails_closed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
            stdout_truncated=True,
        ),
    )

    with pytest.raises(RunnerProtocolError, match="truncated") as error:
        OpenCodeRunner().run(_request(context))
    assert getattr(error.value, "session_id", None) is None


def test_finalize_reports_deleted_session_and_private_transcript(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(0, '{"session":"session-1"}\n', "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult("[]", 0, "", "", SessionMetadata("session-1", "persisted", True))

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.resumable is False
    assert finalized.session.transcript is not None
    assert pathlib.Path(finalized.session.transcript).is_relative_to(context.git_dir)
    assert pathlib.Path(finalized.session.transcript).read_text(encoding="utf-8") == '{"session":"session-1"}\n'
    assert [call[1:3] for call in calls] == [["export", "session-1"], ["session", "delete"]]


def test_finalize_never_persists_truncated_export(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    def fake_run_process(argv, **kwargs):
        if argv[1] == "export":
            return ProcessResult(
                0,
                '{"session":"session-truncated"}\n',
                "",
                stdout_truncated=True,
            )
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-truncated", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert list((context.git_dir / "ai-push-hooks" / "transcripts").iterdir()) == []
