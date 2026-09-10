from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import replace

import pytest

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.ask import (
    OPENCODE_APPLY_AGENT,
    OPENCODE_READ_ONLY_AGENT,
    call_opencode,
    finalize_opencode_session,
    run_ask_step,
)
from ai_push_hooks.executors.runners import (
    RunnerAdapterUnavailableError,
    RunnerCapabilities,
    RunnerResult,
    RunnerTimeoutError,
    SessionMetadata,
)
from ai_push_hooks.types import HookError

from .conftest import build_context, init_repo


def _use_runner_boundary_logger(context, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def llm_call(stage, purpose, model, attempt=None, total_attempts=None, **fields):
        number = len(calls) + 1
        calls.append(
            {
                "call_number": number,
                "stage": stage,
                "purpose": purpose,
                "model": model,
                "attempt": attempt,
                "total_attempts": total_attempts,
                **fields,
            }
        )
        return number

    completions: list[dict[str, object]] = []

    def llm_complete(call_number, stage, profile, runner_type, **fields):
        completions.append(
            {
                "call_number": call_number,
                "stage": stage,
                "profile": profile,
                "runner_type": runner_type,
                **fields,
            }
        )

    monkeypatch.setattr(context.logger, "llm_call", llm_call)
    monkeypatch.setattr(context.logger, "llm_complete", llm_complete, raising=False)
    monkeypatch.setattr(context.logger, "completions", completions, raising=False)


def test_run_ask_step_accepts_array_for_docs_issue_schema(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    analyze_step = next(step for step in config.modules["docs"].steps if step.id == "analyze")
    analyze_step = replace(analyze_step, inputs=())

    class FakeRunner:
        capabilities = RunnerCapabilities()

        def run(self, request):
            assert request.runner_type == "opencode"
            assert request.mode == "ask"
            return RunnerResult("[]", 0, "", "")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())

    payload = run_ask_step(context, analyze_step, "prompt", [], "docs.analyze")

    assert payload == []


def test_call_opencode_constructs_command_with_explicit_agent(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    original_data_home = tmp_path / "opencode-data"
    host_home = tmp_path / "host-home"
    host_config = tmp_path / "host-config"
    (host_home / ".opencode").mkdir(parents=True)
    (host_home / ".opencode" / "opencode.json").write_text(
        '{"share":"auto","mcp":{"unsafe":{"type":"local","command":["sh"]}}}',
        encoding="utf-8",
    )
    (host_config / "opencode").mkdir(parents=True)
    (host_config / "opencode" / "opencode.json").write_text(
        '{"share":"auto","plugin":["unsafe-plugin"]}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(original_data_home))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "malicious.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"share":"auto"}')
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret@proxy.invalid")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    captured: dict[str, object] = {}

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        captured["inherit_env"] = kwargs["inherit_env"]
        captured["cwd"] = kwargs["cwd"]
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    call_opencode(context, "docs.query", "ask:query", "prompt", [], agent="read-only")

    assert captured["args"][:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        OPENCODE_READ_ONLY_AGENT,
        "--pure",
        "--format",
        "json",
    ]
    env = captured["env"]
    assert env["OPENCODE_PURE"] == "true"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "true"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in env
    assert env["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"
    assert env["OPENCODE_DISABLE_SHARE"] == "true"
    assert "OPENCODE_CONFIG" not in env
    assert pathlib.Path(env["HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_CONFIG_HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_DATA_HOME"]) == original_data_home.resolve()
    assert env["OPENAI_API_KEY"] == "provider-key"
    assert "HTTPS_PROXY" not in env
    assert "UNRELATED_SECRET" not in env
    assert captured["inherit_env"] is False
    assert not pathlib.Path(captured["cwd"]).is_relative_to(repo.resolve())
    assert captured["cwd_entries"] == []
    security_config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert security_config["plugin"] == []
    assert security_config["mcp"] == {}
    assert security_config["share"] == "disabled"
    assert security_config["instructions"] == []
    assert security_config["formatter"] is False
    assert security_config["lsp"] is False
    assert security_config["command"] == {}
    permissions = security_config["agent"][OPENCODE_READ_ONLY_AGENT]["permission"]
    assert permissions["*"] == "deny"
    assert permissions["read"] == "deny"
    assert permissions["glob"] == "deny"
    assert permissions["grep"] == "deny"
    assert permissions["list"] == "deny"
    for denied in (
        "edit",
        "bash",
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "lsp",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[denied] == "deny"


def test_call_opencode_apply_config_uses_edit_permission_for_all_mutating_tools(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    staging = tmp_path / "Temp Root" / "OpenCode-Staging"
    staging.mkdir(parents=True)
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(staging, target_is_directory=True)
    captured: dict[str, object] = {}

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    call_opencode(
        context,
        "docs.apply",
        "apply:apply",
        "prompt",
        [],
        agent="apply",
        allow_paths=("README.md", "docs/**/*.md"),
        working_directory=staging_link,
    )

    assert captured["args"][2:6] == ["--agent", OPENCODE_APPLY_AGENT, "--pure", "--format"]
    security_config = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    permissions = security_config["agent"][OPENCODE_APPLY_AGENT]["permission"]
    assert permissions["*"] == "deny"
    assert permissions["read"] == "allow"
    assert permissions["glob"] == "deny"
    assert permissions["grep"] == "deny"
    assert permissions["list"] == "deny"
    prefix = staging.resolve().relative_to(pathlib.Path(staging.anchor)).as_posix()
    assert permissions["edit"] == {
        "*": "deny",
        f"{prefix}/README.md": "allow",
        f"{prefix}/docs/*.md": "allow",
        f"{prefix}/docs/**/*.md": "allow",
        f"{prefix}/.git": "deny",
        f"{prefix}/.git/**": "deny",
    }
    # OpenCode 1.18.29's write and edit tools both request the `edit`
    # permission; a separate `write` grant would be ineffective and broader
    # than the documented permission contract.
    assert "write" not in permissions
    for denied in ("bash", "task", "external_directory", "webfetch", "websearch"):
        assert permissions[denied] == "deny"


def test_call_opencode_apply_requires_isolated_staging_directory(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)

    with pytest.raises(HookError, match="requires an isolated staging directory"):
        call_opencode(
            context,
            "docs.apply",
            "apply:apply",
            "prompt",
            [],
            agent="apply",
            allow_paths=("README.md",),
        )


@pytest.mark.parametrize("directory_kind", ["missing", "file"])
def test_call_opencode_apply_rejects_missing_or_non_directory_working_directory(
    tmp_path: pathlib.Path, directory_kind: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    working_directory = tmp_path / "not-a-directory"
    if directory_kind == "file":
        working_directory.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(HookError, match="existing directory"):
        call_opencode(
            context,
            "docs.apply",
            "apply:apply",
            "prompt",
            [],
            agent="apply",
            allow_paths=("README.md",),
            working_directory=working_directory,
        )


def test_finalize_session_exports_from_private_scratch_and_deletes_same_session(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[tuple[list[str], pathlib.Path]] = []
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    def fake_run_command(args, **kwargs):
        cwd = pathlib.Path(kwargs["cwd"])
        calls.append((args, cwd))
        assert not cwd.is_relative_to(repo.resolve())
        assert list(cwd.iterdir()) == []
        assert kwargs["inherit_env"] is False
        assert kwargs["env"]["OPENCODE_PURE"] == "true"
        assert pathlib.Path(kwargs["env"]["HOME"]).is_relative_to(context.run_dir)
        assert "OPENCODE_CONFIG" not in kwargs["env"]
        if args[1] == "export":
            return subprocess.CompletedProcess(
                args, 0, stdout='{"session":"session-1"}\n', stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.apply", "session-1")

    assert [args[1:3] for args, _ in calls] == [
        ["export", "session-1"],
        ["session", "delete"],
    ]
    transcript = next((context.git_dir / "ai-push-hooks" / "transcripts").iterdir())
    assert transcript.read_text(encoding="utf-8") == '{"session":"session-1"}\n'
    assert not (repo / ".git" / "opencode").exists()
    status_after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status_after == status_before


@pytest.mark.parametrize(
    ("return_code", "stdout"),
    [(1, "export failed\n"), (0, "")],
)
def test_finalize_session_warns_and_deletes_when_export_returns_false(
    tmp_path: pathlib.Path,
    monkeypatch,
    capsys,
    return_code: int,
    stdout: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        if args[1] == "export":
            return subprocess.CompletedProcess(args, return_code, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.query", "session-failed")

    assert [args[1:3] for args in commands] == [
        ["export", "session-failed"],
        ["session", "delete"],
    ]
    assert list((context.git_dir / "ai-push-hooks" / "transcripts").iterdir()) == []
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_warns_and_deletes_when_export_raises(
    tmp_path: pathlib.Path, monkeypatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        if args[1] == "export":
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.query", "session-timeout")

    assert [args[1:3] for args in commands] == [
        ["export", "session-timeout"],
        ["session", "delete"],
    ]
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_warns_and_deletes_when_transcript_write_raises(
    tmp_path: pathlib.Path, monkeypatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    commands: list[list[str]] = []

    def fake_run_command(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"session":"session-write"}\n', stderr=""
        )

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)
    monkeypatch.setattr(
        "ai_push_hooks.executors.ask.write_text_no_follow",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated transcript failure")),
    )

    finalize_opencode_session(context, "docs.query", "session-write")

    assert [args[1:3] for args in commands] == [
        ["export", "session-write"],
        ["session", "delete"],
    ]
    assert "Could not capture the OpenCode transcript" in capsys.readouterr().err


def test_finalize_session_delete_runs_outside_repository(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=False),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    captured: dict[str, object] = {}

    def fake_run_command(args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("ai_push_hooks.executors.ask.run_command", fake_run_command)

    finalize_opencode_session(context, "docs.apply", "session-1")

    assert captured["args"][1:3] == ["session", "delete"]
    assert not pathlib.Path(captured["cwd"]).is_relative_to(repo.resolve())
    assert captured["cwd_entries"] == []


def test_run_ask_step_always_selects_read_only_agent_policy(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    query_step = replace(query_step, inputs=())
    requests = []

    class FakeRunner:
        capabilities = RunnerCapabilities()

        def run(self, request):
            requests.append(request)
            return RunnerResult("[]", 0, "", "")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert requests[0].runner_type == "opencode"
    assert requests[0].mode == "ask"


def test_call_opencode_rejects_external_and_symlinked_attachments(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    external = repo / "input.txt"
    external.write_text("external", encoding="utf-8")

    with pytest.raises(HookError, match="not a hook-owned artifact"):
        call_opencode(
            context,
            "docs.query",
            "ask:query",
            "prompt",
            [external],
            agent="read-only",
        )

    artifact = context.run_dir / "input.txt"
    artifact.write_text("artifact", encoding="utf-8")
    symlink = context.run_dir / "linked-input.txt"
    symlink.symlink_to(artifact)
    with pytest.raises(HookError, match="symlink"):
        call_opencode(
            context,
            "docs.query",
            "ask:query",
            "prompt",
            [symlink],
            agent="read-only",
        )


def test_json_retry_new_session_finalizes_each_attempt(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    query_step = replace(query_step, inputs=())
    results = iter([RunnerResult("not json", 0, "", "", SessionMetadata("session-1", "persisted", True)), RunnerResult("[]", 0, "", "", SessionMetadata("session-2", "persisted", True))])
    requests = []
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, request):
            requests.append(request)
            return next(results)

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert [request.session_id for request in requests] == [None, None]
    assert finalized == ["session-1", "session-2"]


def test_json_retry_reused_session_finalizes_only_after_last_attempt(
    tmp_path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, json_retry_new_session=False),
    )
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    query_step = replace(query_step, inputs=())
    results = iter([RunnerResult("not json", 0, "", "", SessionMetadata("session-1", "persisted", True)), RunnerResult("[]", 0, "", "", SessionMetadata("session-1", "persisted", True))])
    requests = []
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, request):
            requests.append(request)
            return next(results)

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner())

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert [request.session_id for request in requests] == [None, "session-1"]
    assert [request.resume_session for request in requests] == [False, True]
    assert finalized == ["session-1"]


@pytest.mark.parametrize("second_outcome", [RunnerTimeoutError("timed out"), "missing-output"])
def test_reused_session_is_finalized_when_retry_fails_without_session_metadata(
    tmp_path: pathlib.Path, monkeypatch, second_outcome
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, json_retry_new_session=False))
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    query_step = replace(query_step, inputs=())
    finalized: list[str | None] = []
    outcomes = iter(
        [
            RunnerResult(
                "not json",
                0,
                "",
                "",
                SessionMetadata("session-1", "persisted", True),
            ),
            second_outcome,
        ]
    )

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, request):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "missing-output":
                return RunnerResult("", 0, "", "")
            return outcome

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    with pytest.raises(HookError, match=r"opencode.*docs\.query"):
        run_ask_step(context, query_step, "prompt", [], "docs.query")
    assert finalized == ["session-1"]


def test_reused_session_is_finalized_when_next_runner_construction_fails(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, json_retry_new_session=False))
    context = build_context(repo, config)
    query_step = next(step for step in config.modules["docs"].steps if step.id == "query")
    query_step = replace(query_step, inputs=())
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, _request):
            return RunnerResult(
                "not json",
                0,
                "",
                "",
                SessionMetadata("session-1", "persisted", True),
            )

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    calls = 0

    def get_runner(_type):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeRunner()
        raise RunnerAdapterUnavailableError("construction failed")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr("ai_push_hooks.executors.runner_workflow.get_runner", get_runner)

    with pytest.raises(HookError, match=r"opencode.*docs\.query"):
        run_ask_step(context, query_step, "prompt", [], "docs.query")
    assert finalized == ["session-1"]
