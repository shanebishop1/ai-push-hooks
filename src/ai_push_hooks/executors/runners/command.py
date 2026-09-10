"""Shell-free command runner adapter."""

from __future__ import annotations

import math
import os
import pathlib
import re

from .contracts import (
    RunnerCapabilities,
    RunnerContractError,
    RunnerExecutableNotFoundError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    require_final_text,
    require_zero_exit,
)
from .process import run_process


_ALLOWED_PLACEHOLDERS = frozenset({"{prompt}", "{model}", "{cwd}", "{stage}"})
_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]*\}")


def _is_plain_text(value: object, *, allow_empty: bool = False) -> bool:
    return isinstance(value, str) and (allow_empty or bool(value.strip())) and not any(
        ord(character) < 32 for character in value
    )


def _validate_request(request: object) -> RunnerRequest:
    """Re-check adapter-specific invariants at the process boundary.

    ``RunnerRequest`` validates normal construction.  This second check is
    intentional: callers can still pass objects assembled by deserializers or
    mutate frozen instances with low-level Python APIs before invoking an
    adapter.
    """

    if not isinstance(request, RunnerRequest):
        raise RunnerContractError("command runner requires a RunnerRequest")

    for value, label in (
        (request.profile_id, "profile_id"),
        (request.stage, "stage"),
        (request.purpose, "purpose"),
    ):
        if not _is_plain_text(value):
            raise RunnerContractError(f"{label} must be a non-empty NUL-free string")
    if request.runner_type != "command":
        raise RunnerContractError("command runner requires runner_type 'command'")
    if request.mode not in {"ask", "apply"}:
        raise RunnerContractError("mode must be 'ask' or 'apply'")
    if not isinstance(request.instruction, str) or "\x00" in request.instruction:
        raise RunnerContractError("instruction must be a NUL-free string")
    if any(
        ord(character) < 32 and character not in "\r\n\t"
        for character in request.instruction
    ):
        raise RunnerContractError("instruction contains control characters")

    if not isinstance(request.cwd, pathlib.Path) or "\x00" in str(request.cwd):
        raise RunnerContractError("cwd must be a valid path")
    if (
        isinstance(request.timeout_seconds, bool)
        or not isinstance(request.timeout_seconds, (int, float))
        or not math.isfinite(request.timeout_seconds)
        or request.timeout_seconds <= 0
    ):
        raise RunnerContractError("timeout_seconds must be finite and greater than zero")

    if request.model is not None and not _is_plain_text(request.model):
        raise RunnerContractError("model must be a non-empty NUL-free string when provided")
    if request.variant is not None:
        raise RunnerContractError("variant is not valid for runner type command")
    if request.prompt_transport not in {"stdin", "argv"}:
        raise RunnerContractError("prompt_transport must be 'stdin' or 'argv'")

    command = request.command
    if not isinstance(command, (tuple, list)) or not command:
        raise RunnerContractError("command must be a non-empty argv sequence")

    prompt_count = 0
    for index, argument in enumerate(command, start=1):
        if not isinstance(argument, str) or not argument.strip() or "\x00" in argument:
            raise RunnerContractError(f"command[{index}] must be a non-empty NUL-free string")
        for placeholder in _PLACEHOLDER_PATTERN.findall(argument):
            if placeholder not in _ALLOWED_PLACEHOLDERS:
                raise RunnerContractError(f"unknown placeholder {placeholder!r} in command[{index}]")
        if ("{" in argument or "}" in argument) and argument not in _ALLOWED_PLACEHOLDERS:
            raise RunnerContractError(
                f"placeholders in command[{index}] must be whole argv elements"
            )
        if argument == "{prompt}":
            prompt_count += 1

    if request.prompt_transport == "stdin" and prompt_count:
        raise RunnerContractError("command must not contain {prompt} with stdin transport")
    if request.prompt_transport == "argv" and prompt_count != 1:
        raise RunnerContractError(
            "command must contain exactly one {prompt} with argv transport"
        )
    if "{model}" in command and not request.model:
        raise RunnerContractError("command uses {model} but model is not configured")
    return request


def _render_argv(request: RunnerRequest, packet: str) -> tuple[str, ...]:
    replacements = {
        "{prompt}": packet,
        "{model}": request.model or "",
        "{cwd}": str(request.cwd),
        "{stage}": request.stage,
    }
    return tuple(replacements.get(argument, argument) for argument in request.command)


class CommandRunner:
    """Execute a configured command as a direct argv vector, never via a shell."""

    capabilities = RunnerCapabilities()

    def run(self, request: RunnerRequest) -> RunnerResult:
        request = _validate_request(request)
        packet = request.prompt_packet().render()
        argv = _render_argv(request, packet)

        # ``None`` tells subprocess to inherit the invoking user's complete
        # environment.  argv transport still receives an explicit empty input
        # stream so the child observes EOF rather than an open stdin pipe.
        input_text = packet if request.prompt_transport == "stdin" else ""
        try:
            process_result = run_process(
                argv,
                cwd=request.cwd,
                input_text=input_text,
                timeout_seconds=request.timeout_seconds,
                env=None,
            )
        except RunnerExecutableNotFoundError as exc:
            # The rendered argv can contain prompt/model values.  Do not
            # repeat the lower-level adapter's executable detail here.
            raise RunnerExecutableNotFoundError("runner executable was not found") from exc
        result = RunnerResult(
            final_text=process_result.stdout,
            returncode=process_result.returncode,
            stdout=process_result.stdout,
            stderr=process_result.stderr,
        )
        require_zero_exit(
            request,
            result,
            env=os.environ,
            secrets=tuple(value for value in (request.model,) if value),
        )
        if process_result.stdout_truncated or process_result.stderr_truncated:
            streams = []
            if process_result.stdout_truncated:
                streams.append("stdout")
            if process_result.stderr_truncated:
                streams.append("stderr")
            raise RunnerProtocolError(
                "command runner output exceeded its capture limit",
                details=" and ".join(streams),
            )
        require_final_text(result.final_text, mode=request.mode)
        return result


def create_runner() -> CommandRunner:
    """Construct the stateless command adapter."""

    return CommandRunner()


__all__ = ["CommandRunner", "create_runner"]
