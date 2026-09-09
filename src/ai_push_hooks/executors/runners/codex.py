"""Codex CLI runner adapter.

The Codex JSON mode is a JSONL event stream rather than a single response.
Only completed agent-message items are considered model output, and a process
is successful only after a successful terminal turn and a zero exit status.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .contracts import (
    RunnerCapabilities,
    RunnerContractError,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
    request_sensitive_diagnostics,
    require_final_text,
    require_zero_exit,
)
from .process import ProcessResult, run_process


CODEX_EXECUTABLE = "codex"


@dataclass(frozen=True)
class _CodexOutput:
    final_text: str
    thread_id: str | None
    terminal_success: bool
    terminal_failure: bool


def _protocol_error(
    request: RunnerRequest,
    process: ProcessResult,
    reason: str,
) -> RunnerProtocolError:
    """Build a bounded diagnostic without echoing prompt or environment data."""

    details = request_sensitive_diagnostics(
        request,
        process.stdout,
        process.stderr,
        env=os.environ,
    )
    return RunnerProtocolError(
        f"Codex runner {request.profile_id!r} ({request.runner_type}) "
        f"failed at {request.stage!r}: {reason}",
        details=details,
    )


def _parse_codex_jsonl(request: RunnerRequest, process: ProcessResult) -> _CodexOutput:
    """Parse Codex's additive JSONL event stream.

    Unknown event types are intentionally ignored.  Known event types with an
    invalid shape are protocol failures, since accepting them could turn a
    partial stream into a false success.
    """

    final_text = ""
    thread_id: str | None = None
    terminal_success = False
    terminal_failure = False
    turn_open = False

    for line_number, line in enumerate(process.stdout.splitlines(), start=1):
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _protocol_error(
                request,
                process,
                f"Codex emitted malformed JSONL at line {line_number}",
            ) from exc
        if not isinstance(event, dict):
            raise _protocol_error(
                request,
                process,
                f"Codex JSONL event at line {line_number} was not an object",
            )

        event_type = event.get("type")
        if not isinstance(event_type, str):
            # Future event formats may add fields, but every event still needs
            # the discriminator used by the documented JSONL protocol.
            raise _protocol_error(
                request,
                process,
                f"Codex JSONL event at line {line_number} had no type",
            )

        if event_type == "thread.started":
            value = event.get("thread_id")
            if not isinstance(value, str) or not value.strip():
                raise _protocol_error(
                    request,
                    process,
                    "Codex thread.started event had no thread id",
                )
            thread_id = value
        elif event_type == "turn.started":
            if turn_open:
                raise _protocol_error(request, process, "Codex JSONL turn order was invalid")
            turn_open = True
            if not terminal_failure:
                terminal_success = False
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise _protocol_error(request, process, "Codex item.completed event had no item")
            if item.get("type") == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise _protocol_error(
                        request,
                        process,
                        "Codex agent_message item had no text",
                    )
                # Multiple completed messages are additive; the last one is
                # the final response for this invocation.
                final_text = text
        elif event_type == "turn.completed":
            status = event.get("status")
            turn_open = False
            if isinstance(status, str) and status.lower() not in {
                "completed",
                "success",
                "succeeded",
            }:
                terminal_failure = True
                terminal_success = False
            elif event.get("error"):
                terminal_failure = True
                terminal_success = False
            elif not terminal_failure:
                terminal_success = True
        elif event_type in {"turn.failed", "error"}:
            terminal_failure = True
            terminal_success = False
            turn_open = False

    return _CodexOutput(final_text, thread_id, terminal_success, terminal_failure)


@dataclass(frozen=True)
class CodexRunner:
    """Run one fresh, ephemeral Codex CLI invocation."""

    executable: str = CODEX_EXECUTABLE
    capabilities: RunnerCapabilities = RunnerCapabilities()

    def run(self, request: RunnerRequest) -> RunnerResult:
        if request.runner_type != "codex":
            raise RunnerContractError(
                f"Codex runner cannot handle runner type {request.runner_type!r}"
            )

        sandbox = "read-only" if request.mode == "llm" else "workspace-write"
        argv = [
            self.executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--cd",
            str(request.cwd),
        ]
        if request.model:
            argv.extend(("--model", request.model))

        # Artifact-only analysis runs in a scratch directory and apply always
        # runs in a non-VCS staging projection.  Both need this flag; a
        # project-aware analysis run normally has a Git repository available.
        if request.mode == "apply" or (
            request.mode == "llm" and request.project_access == "artifacts"
        ):
            argv.append("--skip-git-repo-check")
        argv.append("-")

        process = run_process(
            argv,
            cwd=request.cwd,
            input_text=request.prompt_packet().render(),
            timeout_seconds=request.timeout_seconds,
            env=None,
        )
        result = RunnerResult(
            final_text="",
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
        )
        require_zero_exit(
            request,
            result,
            env=os.environ,
        )

        if process.stdout_truncated or process.stderr_truncated:
            raise _protocol_error(request, process, "Codex output stream was truncated")
        if not process.stdout.strip():
            raise _protocol_error(request, process, "Codex emitted an empty JSONL stream")

        parsed = _parse_codex_jsonl(request, process)
        if not parsed.terminal_success:
            reason = (
                "Codex terminal turn failed"
                if parsed.terminal_failure
                else "Codex JSONL stream had no successful terminal turn"
            )
            raise _protocol_error(request, process, reason)

        try:
            final_text = require_final_text(parsed.final_text, mode=request.mode)
        except RunnerMissingOutputError as exc:
            details = request_sensitive_diagnostics(
                request,
                process.stdout,
                process.stderr,
                env=os.environ,
            )
            raise RunnerMissingOutputError(str(exc), details=details) from exc

        return RunnerResult(
            final_text=final_text,
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            session=SessionMetadata(
                session_id=parsed.thread_id,
                state="ephemeral",
                resumable=False,
            ),
        )


def create_runner() -> CodexRunner:
    """Construct the built-in Codex adapter for the static runner registry."""

    return CodexRunner()
