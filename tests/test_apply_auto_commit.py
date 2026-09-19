from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import replace

import pytest

import ai_push_hooks.executors.apply as apply_executor
from ai_push_hooks.config import load_config
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.executors.autocommit import (
    DEFAULT_COMMIT_SUBJECT,
    commit_subject_from_response,
    finalize_pending_auto_commits,
    combined_superseded_message,
)
from ai_push_hooks.executors.runners.contracts import RunnerResult
from ai_push_hooks.types import HookError, ModuleRuntimeState, PushSupersededError

from .conftest import build_context, init_repo


def _issues_artifact(context) -> pathlib.Path:
    path = context.run_dir / "issues.json"
    path.write_text('[{"file":"README.md","description":"stale"}]\n', encoding="utf-8")
    return path


def _run(context, step, input_path):
    step = replace(step, inputs=("issues.json",))
    return run_apply_step(
        context,
        ModuleRuntimeState(module=context.config.modules["docs"]),
        step,
        "apply prompt",
        [input_path],
        "docs.apply",
    )


def _editing_runner(response: str = "", text: str = "# Updated\n"):
    def fake_runner(*args, **kwargs):
        (kwargs["working_directory"] / "README.md").write_text(text, encoding="utf-8")
        return RunnerResult(
            final_text=response, returncode=0, stdout=response, stderr=""
        )

    return fake_runner


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _head_subject(repo: pathlib.Path) -> str:
    return _git(repo, "log", "-1", "--format=%s")


def _run_and_finalize(context, step, input_path):
    """Apply, then run the end-of-workflow finalization the hook performs."""
    outcome = _run(context, step, input_path)
    commits = finalize_pending_auto_commits(context)
    return outcome, (commits[0] if commits else None)


def _apply_step(repo: pathlib.Path, **overrides):
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], **overrides)
    return context, step


# --- default behavior --------------------------------------------------------


def test_auto_commit_defaults_off_and_leaves_edits_uncommitted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    context, step = _apply_step(repo)
    before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    outcome = _run(context, step, _issues_artifact(context))

    assert outcome["changed_files"] == ["README.md"]
    assert "auto_commit" not in outcome
    assert "superseded_message" not in outcome
    assert _git(repo, "rev-parse", "HEAD") == before
    assert "README.md" in _git(repo, "status", "--porcelain")


# --- committing --------------------------------------------------------------


def test_auto_commit_commits_only_the_files_apply_propagated(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    # An unrelated dirty file that the apply step never touched.
    (repo / "src" / "app.py").write_text("print('local wip')\n", encoding="utf-8")
    context, step = _apply_step(repo, auto_commit=True)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    outcome, commit = _run_and_finalize(context, step, _issues_artifact(context))

    assert outcome["auto_commit"] == {"pending": True, "auto_push": False}
    assert commit["committed"] is True
    assert commit["pushed"] is False
    assert commit["committed_files"] == ["README.md"]
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["README.md"]
    # The unrelated edit stays in the working tree rather than being swept in.
    assert "src/app.py" in _git(repo, "status", "--porcelain")


def test_auto_commit_reports_the_push_as_superseded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    context, step = _apply_step(repo, auto_commit=True)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    _, commit = _run_and_finalize(context, step, _issues_artifact(context))
    message = combined_superseded_message([commit])

    assert "push 1" in message
    assert "FAILED" in message
    assert "push 2" not in message  # nothing was pushed without auto_push
    assert "Run `git push` again" in message


# --- who names the commit ----------------------------------------------------


def test_runner_may_author_the_commit_subject(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    context, step = _apply_step(repo, auto_commit=True)
    monkeypatch.setattr(
        apply_executor,
        "run_runner_once",
        _editing_runner("Fixed the drift.\nCOMMIT: docs: correct the install command"),
    )

    _, commit = _run_and_finalize(context, step, _issues_artifact(context))

    assert commit["subject_source"] == "runner"
    assert _head_subject(repo) == "docs: correct the install command"


def test_apply_prompt_requests_a_subject_only_when_one_is_needed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    seen: list[str] = []

    def capture(*args, **kwargs):
        seen.append(args[2])
        return _editing_runner()(*args, **kwargs)

    monkeypatch.setattr(apply_executor, "run_runner_once", capture)

    context, step = _apply_step(repo)
    _run(context, step, _issues_artifact(context))
    assert "COMMIT:" not in seen[-1]

    repo2 = init_repo(tmp_path / "b", branch="feature/docs")
    context, step = _apply_step(repo2, auto_commit=True)
    _run(context, step, _issues_artifact(context))
    assert "COMMIT:" in seen[-1]

    repo3 = init_repo(tmp_path / "c", branch="feature/docs")
    context, step = _apply_step(repo3, auto_commit=True, commit_message="docs: fixed")
    _run(context, step, _issues_artifact(context))
    assert "COMMIT:" not in seen[-1]


def test_configured_commit_message_wins_over_the_runner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    context, step = _apply_step(
        repo, auto_commit=True, commit_message="docs: configured subject"
    )
    monkeypatch.setattr(
        apply_executor, "run_runner_once", _editing_runner("COMMIT: runner subject")
    )

    _, commit = _run_and_finalize(context, step, _issues_artifact(context))

    assert commit["subject_source"] == "config"
    assert _head_subject(repo) == "docs: configured subject"


def test_missing_subject_line_falls_back_to_the_default(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    context, step = _apply_step(repo, auto_commit=True)
    monkeypatch.setattr(
        apply_executor, "run_runner_once", _editing_runner("I fixed it. No directive.")
    )

    _, commit = _run_and_finalize(context, step, _issues_artifact(context))

    assert commit["subject_source"] == "default"
    assert _head_subject(repo) == DEFAULT_COMMIT_SUBJECT


@pytest.mark.parametrize(
    "proposed",
    [
        "COMMIT: -delete-everything",  # would read as a git option
        "COMMIT: " + "x" * 200,  # unbounded length
        "COMMIT: ",  # empty
        "COMMIT: subject\x1b[31mwith-control-chars",  # terminal control sequence
    ],
)
def test_unusable_runner_subjects_are_discarded(proposed: str) -> None:
    assert commit_subject_from_response(proposed) is None


def test_runner_subject_cannot_inject_a_second_directive_line() -> None:
    # Only the final directive is honored, and it is still held to the rules.
    assert commit_subject_from_response("COMMIT: first\nCOMMIT: second") == "second"


# --- a superseded push is never allowed through -------------------------------


def _superseding_run(*args, **kwargs):
    raise PushSupersededError("Committed abc1234; the push was superseded.")


@pytest.mark.parametrize("fail_open", ["env", "config"])
def test_superseded_push_ignores_fail_open_settings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, fail_open: str
) -> None:
    # Fail-open exists for checks that could not complete. A commit this run
    # created is not that: letting the push through would send the pre-fix commit
    # and report success, which is the exact failure auto_commit prevents.
    import ai_push_hooks.hook as hook_module

    repo = init_repo(tmp_path, branch="feature/docs")
    if fail_open == "env":
        monkeypatch.setenv("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR", "1")
    else:
        config_path = repo / "ai-push-hooks.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "allow_push_on_error = false", "allow_push_on_error = true"
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(hook_module, "_run_hook_impl", _superseding_run)

    with pytest.raises(PushSupersededError):
        hook_module.run_hook(cwd=repo)


def test_ordinary_errors_still_honor_fail_open(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_push_hooks.hook as hook_module

    repo = init_repo(tmp_path, branch="feature/docs")
    monkeypatch.setenv("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR", "1")

    def boom(*args, **kwargs):
        raise HookError("runner unreachable")

    monkeypatch.setattr(hook_module, "_run_hook_impl", boom)

    assert hook_module.run_hook(cwd=repo) == 0


# --- the commit waits for the whole workflow to pass --------------------------

_AUTO_COMMIT_WORKFLOW = """
[general]
enabled = true
base_branch = "main"
skip_on_sync_branch = false

[workflow]
modules = ["rules"]

[[modules.rules.steps]]
id = "fix"
type = "apply"
prompt = "fix it"
allow_paths = ["README.md"]
auto_commit = true

[[modules.rules.steps]]
id = "verify"
type = "assert"
command = ["{python}", "-c", "import sys; sys.exit(EXIT)"]
"""


def _hook_repo(
    tmp_path: pathlib.Path, *, verify_exit: int, auto_push: bool = False
) -> pathlib.Path:
    repo = init_repo(tmp_path, branch="feature/docs")
    workflow = _AUTO_COMMIT_WORKFLOW.replace("EXIT", str(verify_exit))
    if auto_push:
        workflow = workflow.replace(
            "auto_commit = true", "auto_commit = true\nauto_push = true"
        )
    (repo / "ai-push-hooks.toml").write_text(workflow, encoding="utf-8")
    _git(repo, "commit", "-aqm", "configure workflow")
    return repo


def _push_stdin(repo: pathlib.Path) -> list[str]:
    tip = _git(repo, "rev-parse", "HEAD")
    return [f"refs/heads/feature/docs {tip} refs/heads/feature/docs {'0' * 40}"]


def test_a_failing_later_step_leaves_the_fix_uncommitted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of deferring: a check after `apply` must still be able to
    # reject the edits, which it cannot do if they are already committed.
    import ai_push_hooks.hook as hook_module

    repo = _hook_repo(tmp_path, verify_exit=1)
    before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(HookError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    assert not isinstance(excinfo.value, PushSupersededError)
    assert _git(repo, "rev-parse", "HEAD") == before
    # The edit is still there for the developer to inspect, just not committed.
    assert "README.md" in _git(repo, "status", "--porcelain")


def test_a_passing_workflow_commits_and_supersedes_the_push(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_push_hooks.hook as hook_module

    repo = _hook_repo(tmp_path, verify_exit=0)
    before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError, match="push 1"):
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    assert _git(repo, "rev-parse", "HEAD") != before
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "README.md"
    ]
    assert _git(repo, "status", "--porcelain") == ""


def test_the_commit_is_recorded_in_the_run_summary(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_push_hooks.hook as hook_module

    repo = _hook_repo(tmp_path, verify_exit=0)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError):
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    summaries = sorted((repo / ".git/ai-push-hooks/summaries").glob("*.json"))
    payload = json.loads(summaries[-1].read_text(encoding="utf-8"))
    assert payload["auto_commit"][0]["committed"] is True
    assert payload["auto_commit"][0]["committed_files"] == ["README.md"]


def test_the_hook_never_pushes_the_commit_it_makes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pushing here would put the fix on the remote but still leave the `git push`
    # the developer typed reporting failure, which reads as an error after an
    # operation that worked. The developer re-pushes instead.
    import ai_push_hooks.hook as hook_module

    repo = _hook_repo(tmp_path, verify_exit=0)
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
    )
    _git(repo, "remote", "add", "origin", str(remote))
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError):
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    branches = subprocess.run(
        ["git", "--git-dir", str(remote), "branch", "--list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branches == ""


def test_the_stop_is_machine_detectable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Git collapses every nonzero hook exit to 1, so a script or agent driving
    # `git push` cannot branch on exit status. It matches this marker instead.
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import RETRY_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=0)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    final_line = str(excinfo.value).strip().splitlines()[-1]
    assert final_line.startswith(RETRY_NOTICE)
    assert "Run `git push` again" in final_line
    assert _git(repo, "rev-parse", "--short=12", "HEAD") in final_line


def test_a_plain_block_carries_no_retry_marker(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blocked push must never look like a retryable one, or an agent would
    # re-push forever against a check that is legitimately failing.
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import RETRY_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=1)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(HookError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    assert RETRY_NOTICE not in str(excinfo.value)


# --- auto_push ---------------------------------------------------------------


def _with_remote(repo: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True
    )
    _git(repo, "remote", "add", "origin", str(remote))
    return remote


def _remote_sha(remote: pathlib.Path, ref: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_auto_push_puts_the_fix_on_the_remote_in_one_push(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import PUSHED_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    remote = _with_remote(repo, tmp_path)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    message = str(excinfo.value)
    assert "push 1" in message and "FAILED" in message
    assert "push 2" in message and "SUCCEEDED" in message
    assert message.strip().splitlines()[-1].startswith(PUSHED_NOTICE)
    # The fix really is on the remote, from the single push the developer ran.
    assert _remote_sha(remote, "refs/heads/feature/docs") == _git(
        repo, "rev-parse", "HEAD"
    )


def test_auto_push_failure_falls_back_to_the_retry_marker(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No remote configured, so the nested push cannot succeed. The fix is
    # committed but not sent, so the caller must be told to push, not that it
    # is already done.
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import PUSHED_NOTICE, RETRY_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    message = str(excinfo.value)
    assert RETRY_NOTICE in message
    assert PUSHED_NOTICE not in message
    assert "is NOT on the remote" in message


def test_auto_push_never_reenters_the_hook(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    _with_remote(repo, tmp_path)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    seen: list[tuple[list[str], dict]] = []
    import ai_push_hooks.executors.autocommit as autocommit_module

    real = autocommit_module.run_command

    def spy(args, cwd, **kwargs):
        seen.append((list(args), kwargs))
        return real(args, cwd, **kwargs)

    monkeypatch.setattr(autocommit_module, "run_command", spy)

    with pytest.raises(PushSupersededError):
        hook_module_run(repo)

    pushes = [(a, k) for a, k in seen if a[:2] == ["git", "push"]]
    assert len(pushes) == 1
    args, kwargs = pushes[0]
    assert "--no-verify" in args
    assert kwargs["env"]["AI_PUSH_HOOKS_SKIP"] == "1"


def hook_module_run(repo: pathlib.Path):
    import ai_push_hooks.hook as hook_module

    return hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)


def test_colour_is_suppressed_when_not_a_terminal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An agent captures this output; escape codes would be noise in its context.
    import ai_push_hooks.hook as hook_module

    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    _with_remote(repo, tmp_path)
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    assert "\x1b[" not in str(excinfo.value)


# --- never commit work the developer did not stage ---------------------------


def test_auto_commit_refuses_files_that_were_already_dirty(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `git commit -- <file>` commits the whole file. If the developer already had
    # uncommitted work in it, committing would capture work they never staged --
    # and with auto_push, publish it to the remote.
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import PUSHED_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    remote = _with_remote(repo, tmp_path)
    (repo / "README.md").write_text("# Example\n\nMY UNSTAGED WIP\n", encoding="utf-8")
    before = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    message = str(excinfo.value)
    assert "Refusing to auto-commit" in message
    assert "README.md" in message
    assert PUSHED_NOTICE not in message
    # Nothing committed and nothing pushed. (Whether `apply` itself overwrote the
    # unstaged text is separate, pre-existing behavior; what must never happen is
    # that unstaged work gets captured in a commit and published.)
    assert _git(repo, "rev-parse", "HEAD") == before
    assert (
        subprocess.run(
            ["git", "--git-dir", str(remote), "branch", "--list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == ""
    )


def test_unrelated_dirty_files_still_do_not_block_the_commit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only dirt in a file the fix actually touched is a problem.
    import ai_push_hooks.hook as hook_module
    from ai_push_hooks.executors.autocommit import PUSHED_NOTICE

    repo = _hook_repo(tmp_path, verify_exit=0, auto_push=True)
    _with_remote(repo, tmp_path)
    (repo / "src" / "app.py").write_text("print('wip')\n", encoding="utf-8")
    monkeypatch.setattr(apply_executor, "run_runner_once", _editing_runner())

    with pytest.raises(PushSupersededError) as excinfo:
        hook_module.run_hook(stdin_lines=_push_stdin(repo), cwd=repo)

    assert PUSHED_NOTICE in str(excinfo.value)
    assert "src/app.py" in _git(repo, "status", "--porcelain")
