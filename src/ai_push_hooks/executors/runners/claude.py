"""Claude Code CLI runner.

The adapter deliberately uses Claude's print-mode JSON result rather than its
internal transcript/event files.  Capability discovery is performed once when
the adapter is constructed so a changed CLI cannot silently receive a weaker
permission or persistence policy.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
from typing import Any

from .contracts import (
    RunnerAdapterUnavailableError,
    RunnerCapabilities,
    RunnerContractError,
    RunnerError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
    bounded_redacted_diagnostics,
    request_sensitive_diagnostics,
    require_final_text,
    require_zero_exit,
)
from .process import ProcessResult, run_process


CAPABILITY_CHECK_TIMEOUT_SECONDS = 10
CLAUDE_EXECUTABLE = "claude"
ANALYSIS_TOOLS = "Read,Glob,Grep"
APPLY_TOOLS = "Read,Edit,Write"

# These are intentionally the long, documented spellings used by the
# invocation.  In particular, --allowed-tools alone is not enough evidence
# for using --allowedTools: an installed CLI must advertise the exact spelling
# we execute.
REQUIRED_HELP_MARKERS = (
    "--print",
    "--output-format",
    "json",
    "--no-session-persistence",
    "--model",
    "--permission-mode",
    "dontAsk",
    "acceptEdits",
    "--tools",
    "--allowedTools",
)


def resolve_claude_executable() -> str:
    """Resolve the user-managed Claude executable without invoking it."""

    executable = shutil.which(CLAUDE_EXECUTABLE)
    if executable:
        return executable
    raise RunnerAdapterUnavailableError(
        "Claude Code CLI is required but is not installed"
    )


def _capability_error(
    reason: str, *, details: str = ""
) -> RunnerAdapterUnavailableError:
    return RunnerAdapterUnavailableError(
        "Claude Code CLI does not satisfy the required non-interactive contract",
        details=f"{reason}{(': ' + details) if details else ''}",
    )


def check_claude_capabilities(
    executable: str,
    *,
    cwd: pathlib.Path | None = None,
) -> None:
    """Check required flags and permission modes using ``claude --help`` only.

    This function never supplies a prompt, model, or authentication operation.
    A truncated help response is rejected because it cannot prove that every
    required safety flag is supported.
    """

    check_cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    try:
        help_result = run_process(
            [executable, "--help"],
            cwd=check_cwd,
            input_text=None,
            timeout_seconds=CAPABILITY_CHECK_TIMEOUT_SECONDS,
        )
    except RunnerError as exc:
        raise _capability_error(type(exc).__name__) from exc

    if help_result.returncode != 0:
        details = bounded_redacted_diagnostics(help_result.stdout, help_result.stderr)
        raise _capability_error("help command failed", details=details)
    if help_result.stdout_truncated or help_result.stderr_truncated:
        raise _capability_error("help output was truncated")

    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    missing = tuple(
        marker for marker in REQUIRED_HELP_MARKERS if marker not in help_text
    )
    if missing:
        raise _capability_error(
            "missing required flags or modes", details=", ".join(missing)
        )


def _protocol_failure(
    request: RunnerRequest,
    process_result: ProcessResult,
    reason: str,
    *,
    extra_secrets: tuple[str, ...] = (),
) -> RunnerProtocolError:
    details = request_sensitive_diagnostics(
        request,
        process_result.stdout,
        process_result.stderr,
        env=os.environ,
        extra=extra_secrets,
    )
    return RunnerProtocolError(
        f"Claude returned an invalid result for {request.profile_id!r} "
        f"({request.runner_type}) at {request.stage!r}: {reason}",
        details=details,
    )


def parse_claude_result(
    request: RunnerRequest,
    process_result: ProcessResult,
) -> RunnerResult:
    """Parse one documented top-level Claude print-mode JSON result.

    The parser is additive with respect to metadata: fields such as usage,
    cost, or future provider metadata are ignored.  It is deliberately strict
    about framing and the final result so a bounded/truncated stream cannot be
    mistaken for a successful response.
    """

    if process_result.stdout_truncated or process_result.stderr_truncated:
        streams = []
        if process_result.stdout_truncated:
            streams.append("stdout")
        if process_result.stderr_truncated:
            streams.append("stderr")
        raise _protocol_failure(
            request,
            process_result,
            "output stream was truncated: " + ", ".join(streams),
        )

    raw = process_result.stdout.strip()
    if not raw:
        raise _protocol_failure(request, process_result, "stdout was empty")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _protocol_failure(
            request, process_result, "stdout was not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise _protocol_failure(
            request, process_result, "top-level JSON value was not an object"
        )

    message_type = payload.get("type")
    if message_type != "result":
        raise _protocol_failure(
            request, process_result, "top-level JSON type was not result"
        )

    is_error = payload.get("is_error")
    if not isinstance(is_error, bool):
        raise _protocol_failure(request, process_result, "is_error was not boolean")
    subtype = payload.get("subtype")
    if not isinstance(subtype, str):
        raise _protocol_failure(request, process_result, "subtype was not a string")

    # Claude documents success as subtype=success and uses error_* subtypes for
    # terminal failures.  Treat every explicit non-success subtype as a
    # failure, including future subtypes, rather than accepting an unknown
    # terminal state as a response.
    if is_error or subtype != "success":
        # Never copy provider/model-controlled subtype text into a diagnostic.
        raise _protocol_failure(
            request,
            process_result,
            "terminal result was not successful",
            extra_secrets=(subtype,),
        )

    final_text = payload.get("result")
    if not isinstance(final_text, str):
        raise _protocol_failure(request, process_result, "result was not a string")

    session_id = payload.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise _protocol_failure(request, process_result, "session_id was not a string")
    if isinstance(session_id, str) and not session_id.strip():
        raise _protocol_failure(request, process_result, "session_id was empty")

    return RunnerResult(
        final_text=require_final_text(final_text, mode=request.mode),
        returncode=process_result.returncode,
        stdout=process_result.stdout,
        stderr=process_result.stderr,
        session=SessionMetadata(
            session_id=session_id,
            state="ephemeral",
            resumable=False,
        ),
    )


def build_claude_argv(executable: str, request: RunnerRequest) -> list[str]:
    """Build the exact shell-free argv contract for one Claude invocation."""

    if not isinstance(request, RunnerRequest):
        raise RunnerContractError("Claude runner requires a RunnerRequest")
    if request.runner_type != "claude":
        raise RunnerContractError(
            f"Claude runner cannot handle runner type {request.runner_type!r}"
        )
    if request.mode not in {"ask", "apply"}:
        raise RunnerContractError("Claude runner request mode must be 'ask' or 'apply'")

    tools = ANALYSIS_TOOLS if request.mode == "ask" else APPLY_TOOLS
    argv = [
        executable,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    if request.model is not None and request.model.strip():
        argv.extend(["--model", request.model])
    argv.extend(
        [
            "--permission-mode",
            "dontAsk" if request.mode == "ask" else "acceptEdits",
            "--tools",
            tools,
            "--allowedTools",
            tools,
        ]
    )
    return argv


class ClaudeRunner:
    """Run Claude Code with an ephemeral, stage-specific tool policy."""

    capabilities = RunnerCapabilities()

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def run(self, request: RunnerRequest) -> RunnerResult:
        argv = build_claude_argv(self.executable, request)
        packet = request.prompt_packet().render()
        process_result = run_process(
            argv,
            cwd=request.cwd,
            input_text=packet,
            timeout_seconds=request.timeout_seconds,
            # None explicitly means inherit the user's normal Claude login and
            # provider environment.  The environment is never logged.
            env=None,
        )
        normalized = RunnerResult(
            final_text="",
            returncode=process_result.returncode,
            stdout=process_result.stdout,
            stderr=process_result.stderr,
        )
        require_zero_exit(
            request,
            normalized,
            env=os.environ,
        )
        return parse_claude_result(request, process_result)


def create_runner() -> ClaudeRunner:
    """Construct a Claude runner only after proving its CLI capabilities."""

    executable = resolve_claude_executable()
    check_claude_capabilities(executable)
    return ClaudeRunner(executable)
