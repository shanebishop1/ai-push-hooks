#!/usr/bin/env python3
"""Explicitly gated, disposable live probes for Codex and Pi.

The normal path is deliberately inert.  A live invocation needs both
``AI_PUSH_HOOKS_LIVE_PROBE=1`` and an explicit ``--profile``/``--model``.
Apply is a second opt-in and requires a user-selected time budget.  No
credential value is read, serialized, or accepted as an argument.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Sequence


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.executors.llm import run_llm_step
from ai_push_hooks.types import (
    GeneralConfig,
    HookConfig,
    HookLogger,
    LlmConfig,
    LoggingConfig,
    ModuleConfig,
    ModuleRuntimeState,
    PushRefUpdate,
    RunnerProfile,
    RuntimeContext,
    StepConfig,
    WorkflowConfig,
)


LIVE_OPT_IN = "AI_PUSH_HOOKS_LIVE_PROBE"
LIVE_APPLY_OPT_IN = "AI_PUSH_HOOKS_LIVE_APPLY"
MAX_TIMEOUT_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 180
NONCE_FILENAME = "runner-live-probe-nonce.txt"
README_FILENAME = "README.md"
OUTSIDE_FILENAME = "outside-allowlist.txt"
APPLY_MARKER = "runner-live-probe: allowlisted edit"

PI_READ_COMMAND = (
    "pi",
    "--print",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--tools",
    "read,grep,find,ls",
    "--model",
    "{model}",
)
PI_APPLY_COMMAND = (
    "pi",
    "--print",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--tools",
    "read,grep,find,ls,edit,write",
    "--model",
    "{model}",
)


class LiveProbeError(RuntimeError):
    """A safe, non-payload-bearing live probe failure."""


@dataclass(frozen=True)
class ProbeResult:
    profile: str
    model: str
    nonce_verified: bool
    apply_requested: bool
    changed_files: tuple[str, ...]


def _env(env: dict[str, str] | None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    # Read only the two control variables.  Provider credentials are inherited
    # by the selected production adapter at process-spawn time; this harness
    # never inspects or serializes them.
    return {
        LIVE_OPT_IN: os.environ.get(LIVE_OPT_IN, ""),
        LIVE_APPLY_OPT_IN: os.environ.get(LIVE_APPLY_OPT_IN, ""),
    }


def require_live_opt_in(env: dict[str, str] | None = None) -> None:
    if _env(env).get(LIVE_OPT_IN) != "1":
        raise LiveProbeError(
            f"live probe disabled; set {LIVE_OPT_IN}=1 and choose a profile/model explicitly"
        )


def _validate_model(model: str) -> str:
    # Models are identifiers, not command fragments.  This rejects shell
    # syntax, whitespace, option injection, and credential-shaped argv such as
    # ``--api-key=...`` without imposing a provider catalog.
    if not isinstance(model, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}", model
    ):
        raise LiveProbeError("model must be a selected provider/model identifier, not a command")
    if model.startswith("-"):
        raise LiveProbeError("model must not be an option")
    return model


def build_profile(profile: str, model: str, *, apply: bool = False) -> RunnerProfile:
    if profile not in {"codex", "pi"}:
        raise LiveProbeError("profile must be one of: codex, pi")
    model = _validate_model(model)
    if profile == "codex":
        return RunnerProfile(
            name="live-codex",
            type="codex",
            model=model,
            project_access="project",
        )
    return RunnerProfile(
        name="live-pi-apply" if apply else "live-pi-read",
        type="command",
        model=model,
        project_access="project",
        command=PI_APPLY_COMMAND if apply else PI_READ_COMMAND,
        prompt_transport="stdin",
    )


def _git(argv: Sequence[str], cwd: pathlib.Path) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise LiveProbeError("disposable Git project setup failed")
    return completed.stdout.strip()


def create_disposable_project(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    project = root / "project"
    project.mkdir()
    _git(["init", "-b", "main"], project)
    _git(["config", "user.name", "runner-live-probe"], project)
    _git(["config", "user.email", "runner-live-probe@example.invalid"], project)
    nonce = secrets.token_hex(16)
    (project / NONCE_FILENAME).write_text(nonce + "\n", encoding="utf-8")
    (project / README_FILENAME).write_text("# Runner live probe\n", encoding="utf-8")
    (project / OUTSIDE_FILENAME).write_text("must remain unchanged\n", encoding="utf-8")
    _git(["add", NONCE_FILENAME, README_FILENAME, OUTSIDE_FILENAME], project)
    _git(["commit", "-m", "disposable runner probe baseline"], project)
    return project, nonce


def _context(project: pathlib.Path, profile: RunnerProfile, timeout: int) -> RuntimeContext:
    git_dir = project / ".git"
    run_dir = git_dir / "ai-push-hooks-live-probe"
    run_dir.mkdir(parents=True, exist_ok=True)
    head = _git(["rev-parse", "HEAD"], project)
    update = PushRefUpdate(
        local_ref="refs/heads/main",
        local_sha=head,
        remote_ref="refs/heads/main",
        remote_sha="0" * 40,
    )
    module = ModuleConfig("live-probe", True, ())
    config = HookConfig(
        general=GeneralConfig(skip_on_sync_branch=False),
        llm=LlmConfig(
            runner=profile.name,
            model=profile.model or "",
            timeout_seconds=timeout,
            max_parallel=1,
            json_max_retries=0,
            json_retry_new_session=True,
            delete_session_after_run=False,
        ),
        logging=LoggingConfig(
            jsonl=False,
            capture_llm_transcript=False,
            print_llm_output=False,
        ),
        workflow=WorkflowConfig((module.id,)),
        modules={module.id: module},
        runners={profile.name: profile},
    )
    return RuntimeContext(
        repo_root=project,
        git_dir=git_dir,
        config=config,
        logger=HookLogger(None),
        remote_name="origin",
        remote_url="live-probe://disposable",
        stdin_lines=[],
        run_id="runner-live-probe",
        run_dir=run_dir,
        cache={
            "pushed_branch_updates": [update],
            "push_updates": [update],
        },
    )


def run_live_probe(
    profile: str,
    model: str,
    *,
    apply: bool = False,
    apply_budget_seconds: int | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> ProbeResult:
    """Run a selected live read, then an independently opted-in apply."""

    require_live_opt_in(env)
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise LiveProbeError(f"timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds")
    if apply:
        if _env(env).get(LIVE_APPLY_OPT_IN) != "1":
            raise LiveProbeError(
                f"apply probe disabled; set {LIVE_APPLY_OPT_IN}=1 separately"
            )
        if apply_budget_seconds is None or not 1 <= apply_budget_seconds <= MAX_TIMEOUT_SECONDS:
            raise LiveProbeError(
                f"apply requires an explicit budget between 1 and {MAX_TIMEOUT_SECONDS} seconds"
            )
    elif apply_budget_seconds is not None:
        raise LiveProbeError("an apply budget requires --apply")

    read_profile = build_profile(profile, model, apply=False)
    with tempfile.TemporaryDirectory(prefix="ai-push-hooks-runner-probe-") as temporary:
        project, nonce = create_disposable_project(pathlib.Path(temporary))
        context = _context(project, read_profile, timeout_seconds)
        read_step = StepConfig(
            id="read-nonce",
            type="llm",
            runner=read_profile.name,
        )
        response = run_llm_step(
            context,
            read_step,
            f"Read {NONCE_FILENAME} from the current project. Return its exact contents only.",
            [],
            "runner-live-probe.read",
        )
        if nonce not in str(response).strip():
            raise LiveProbeError("selected runner did not return the disposable project nonce")

        changed_files: tuple[str, ...] = ()
        if apply:
            apply_profile = build_profile(profile, model, apply=True)
            apply_context = _context(project, apply_profile, apply_budget_seconds or timeout_seconds)
            apply_module = ModuleConfig("live-probe", True, ())
            apply_step = StepConfig(
                id="allowlisted-edit",
                type="apply",
                runner=apply_profile.name,
                allow_paths=(README_FILENAME,),
            )
            apply_state = ModuleRuntimeState(module=apply_module)
            result = run_apply_step(
                apply_context,
                apply_state,
                apply_step,
                (
                    f"Modify only {README_FILENAME}. Append exactly this line: {APPLY_MARKER}. "
                    "Do not modify, create, delete, or rename any other path."
                ),
                [],
                "runner-live-probe.apply",
            )
            changed_files = tuple(str(path) for path in result.get("changed_files", []))
            if changed_files != (README_FILENAME,):
                raise LiveProbeError("live apply did not produce exactly one allowlisted README edit")
            if APPLY_MARKER not in (project / README_FILENAME).read_text(encoding="utf-8"):
                raise LiveProbeError("live apply did not propagate the expected README marker")
            if (project / OUTSIDE_FILENAME).read_text(encoding="utf-8") != "must remain unchanged\n":
                raise LiveProbeError("live apply changed the outside-allowlist fixture")

        return ProbeResult(profile, model, True, apply, changed_files)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("codex", "pi"), required=True)
    parser.add_argument(
        "--model",
        required=True,
        help="one user-selected provider/model identifier; never a command or credential",
    )
    parser.add_argument("--apply", action="store_true", help="request the separate apply probe")
    parser.add_argument("--apply-budget-seconds", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_live_probe(
            args.profile,
            args.model,
            apply=args.apply,
            apply_budget_seconds=args.apply_budget_seconds,
            timeout_seconds=args.timeout_seconds,
        )
    except LiveProbeError as exc:
        print(f"LIVE PROBE NOT RUN: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - no child payloads in the report
        print(f"LIVE PROBE FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"PASS: {result.profile} selected model {result.model}")
    print("PASS: disposable project nonce verified")
    if result.apply_requested:
        print("PASS: allowlisted apply changed README.md only")
    else:
        print("SKIP: apply probe not requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
