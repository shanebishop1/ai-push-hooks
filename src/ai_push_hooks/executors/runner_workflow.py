"""Runner-neutral orchestration for one workflow model invocation.

The adapters own process invocation and protocol parsing.  This module owns
profile resolution, the common request shape, call accounting, lifecycle
cleanup, and the bounded user-facing error boundary.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from ..config import resolve_runner_profile
from ..types import HookError, RunnerProfile, RuntimeContext, StepConfig
from .runners import (
    Runner,
    RunnerArtifact,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
    bounded_diagnostic,
    finalize_runner,
    get_runner,
    redact_diagnostic,
    require_final_text,
    require_zero_exit,
)
from .runners.contracts import RunnerProtocolError


@dataclass
class _RunnerInvocation:
    request: RunnerRequest
    runner: Runner
    result: RunnerResult
    call_number: int


def _selected_runner_name(context: RuntimeContext, step: StepConfig) -> str:
    return step.runner or context.config.llm.runner


def _safe_input_artifacts(
    context: RuntimeContext,
    step: StepConfig,
    input_paths: list[pathlib.Path],
) -> tuple[RunnerArtifact, ...]:
    """Build the ordered logical artifact list shared by every adapter.

    ``validate_opencode_attachments`` is retained as the hook-owned artifact
    boundary used by the shipped implementation.  The logical names are the
    configured input references, rather than filesystem basenames, so command,
    Codex, Claude, and OpenCode receive the same ordered context.
    """

    from .llm import validate_opencode_attachments

    validated = validate_opencode_attachments(context, input_paths)
    if len(validated) != len(step.inputs):
        raise HookError(
            f"Runner input count does not match configured inputs for step `{step.id}`"
        )
    artifacts: list[RunnerArtifact] = []
    for name, path in zip(step.inputs, validated, strict=True):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HookError(f"Unable to read hook-owned runner artifact: {name}") from exc
        artifacts.append(RunnerArtifact(name=name, content=content, path=path))
    return tuple(artifacts)


def _build_request(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
    working_directory: pathlib.Path,
    *,
    session_id: str | None,
    resume_session: bool,
) -> tuple[RunnerProfile, RunnerRequest]:
    profile = resolve_runner_profile(context.config, step)
    cwd = pathlib.Path(working_directory).resolve(strict=True)
    if not cwd.is_dir():
        raise HookError(f"Runner working directory is not a directory: {working_directory}")
    request = RunnerRequest(
        profile_id=profile.name,
        runner_type=profile.type,
        stage=stage_name,
        purpose=f"{step.type}:{step.id}",
        mode=step.type,  # type: ignore[arg-type]
        instruction=prompt,
        artifacts=_safe_input_artifacts(context, step, input_paths),
        cwd=cwd,
        timeout_seconds=context.config.llm.timeout_seconds,
        model=profile.model,
        variant=profile.variant,
        project_access=profile.project_access,  # type: ignore[arg-type]
        allow_paths=step.allow_paths,
        command=profile.command,
        prompt_transport=profile.prompt_transport,  # type: ignore[arg-type]
        session_id=session_id,
        resume_session=resume_session,
        integration_context=context,
    )
    return profile, request


def _call_logger(
    context: RuntimeContext,
    request: RunnerRequest,
    model: str | None,
    *,
    attempt: int | None,
    total_attempts: int | None,
) -> int:
    return context.logger.llm_call(
        request.stage,
        request.purpose,
        model or "",
        attempt,
        total_attempts,
        runner_profile=request.profile_id,
        runner_type=request.runner_type,
    )


def _session_metadata(runner: Runner, session_id: str) -> SessionMetadata:
    supports_resume = bool(
        getattr(getattr(runner, "capabilities", None), "supports_resume", False)
    )
    return SessionMetadata(
        session_id=session_id,
        state="persisted" if supports_resume else "ephemeral",
        resumable=supports_resume,
    )


def _failure_result(
    runner: Runner,
    error: BaseException,
    fallback_session_id: str | None = None,
) -> RunnerResult:
    session_id = getattr(error, "session_id", None)
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = fallback_session_id
    session = _session_metadata(runner, session_id.strip()) if session_id else None
    return RunnerResult(final_text="", returncode=1, stdout="", stderr="", session=session)


def _preserve_failure_session(
    runner: Runner,
    result: RunnerResult,
    fallback_session_id: str | None,
) -> RunnerResult:
    if (result.session is not None and result.session.session_id) or not fallback_session_id:
        return result
    return RunnerResult(
        final_text=result.final_text,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        session=_session_metadata(runner, fallback_session_id),
        transcript=result.transcript,
    )


def _completion(
    context: RuntimeContext,
    invocation: _RunnerInvocation,
    *,
    failed: bool,
) -> None:
    session = invocation.result.session
    context.logger.llm_complete(
        invocation.call_number,
        invocation.request.stage,
        invocation.request.profile_id,
        invocation.request.runner_type,
        session_id=session.session_id if session else None,
        session_state=session.state if session else None,
        resumable=session.resumable if session else False,
        transcript=session.transcript if session else None,
        # No adapter currently supplies a safe, provider-correct resume
        # command.  In particular, ordinary ``opencode -s`` is invalid
        # for the isolated scratch state used by hook runs.
        resume_command=None,
        failed=failed,
    )


def _print_normalized_output(context: RuntimeContext, invocation: _RunnerInvocation) -> None:
    if not context.config.logging.print_llm_output or not invocation.result.final_text:
        return
    print(
        redact_diagnostic(
            invocation.result.final_text,
            secrets=_request_sensitive_values(invocation.request),
        )
    )


def _request_sensitive_values(request: RunnerRequest) -> tuple[str, ...]:
    environment_values = tuple(
        value
        for name, value in os.environ.items()
        if value
        and any(
            marker in name.upper()
            for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")
        )
    )
    return (
        request.instruction,
        request.prompt_packet().render(),
        *(artifact.content for artifact in request.artifacts),
        *environment_values,
    )


def _named_error(
    profile_name: str,
    runner_type: str,
    stage_name: str,
    error: BaseException,
    *,
    secrets: tuple[str, ...] = (),
) -> HookError:
    details = bounded_diagnostic(str(error), max_chars=1_200, secrets=secrets)
    message = (
        f"Runner profile `{profile_name}` ({runner_type}) failed at stage `{stage_name}`"
    )
    if details:
        message += f": {details}"
    return HookError(message)


def _invoke_runner(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
    *,
    working_directory: pathlib.Path,
    session_id: str | None = None,
    resume_session: bool = False,
    attempt: int | None = None,
    total_attempts: int | None = None,
    prior_invocation: _RunnerInvocation | None = None,
) -> _RunnerInvocation:
    selected_name = _selected_runner_name(context, step)
    profile_name = selected_name
    runner_type = "unknown"
    try:
        profile, request = _build_request(
            context,
            step,
            prompt,
            input_paths,
            stage_name,
            working_directory,
            session_id=session_id,
            resume_session=resume_session,
        )
        profile_name = profile.name
        runner_type = profile.type
        # Adapter construction (including optional CLI capability checks) is
        # deliberately before call accounting.  Counts represent invocations,
        # never capability probes.
        runner = get_runner(profile.type)
        call_number = _call_logger(
            context,
            request,
            profile.model,
            attempt=attempt,
            total_attempts=total_attempts,
        )
    except Exception as exc:  # noqa: BLE001
        if prior_invocation is not None:
            try:
                _finalize_invocation(context, prior_invocation, failed=True)
            except Exception as finalize_error:  # noqa: BLE001
                context.logger.warn(
                    "llm.finalize_failed",
                    "Runner lifecycle finalization failed while cleaning a retained retry session.",
                    stage_name=stage_name,
                    runner_profile=prior_invocation.request.profile_id,
                    runner_type=prior_invocation.request.runner_type,
                    reason=type(finalize_error).__name__,
                )
        raise _named_error(profile_name, runner_type, stage_name, exc) from exc

    result: RunnerResult | None = None
    try:
        result = runner.run(request)
        if not isinstance(result, RunnerResult):
            raise RunnerProtocolError("runner returned an invalid result")
        require_zero_exit(request, result, env=os.environ)
        require_final_text(result.final_text, mode=request.mode)
    except Exception as exc:  # noqa: BLE001
        # A runner may have learned a session ID before reporting a process or
        # protocol failure.  Give the optional lifecycle a chance to clean it
        # up, even though no successful RunnerResult was returned.
        if isinstance(result, RunnerResult):
            result = _preserve_failure_session(runner, result, request.session_id)
        else:
            result = _failure_result(runner, exc, request.session_id)
        invocation = _RunnerInvocation(request, runner, result, call_number)
        try:
            invocation.result = finalize_runner(runner, request, result)
        except Exception as finalize_error:  # noqa: BLE001
            context.logger.warn(
                "llm.finalize_failed",
                "Runner lifecycle finalization failed after a runner error.",
                stage_name=stage_name,
                runner_profile=profile_name,
                runner_type=runner_type,
                reason=type(finalize_error).__name__,
            )
        _completion(context, invocation, failed=True)
        raise _named_error(
            profile_name,
            runner_type,
            stage_name,
            exc,
            secrets=_request_sensitive_values(request),
        ) from exc

    return _RunnerInvocation(request, runner, result, call_number)


def _finalize_invocation(
    context: RuntimeContext,
    invocation: _RunnerInvocation,
    *,
    failed: bool,
) -> RunnerResult:
    try:
        invocation.result = finalize_runner(
            invocation.runner,
            invocation.request,
            invocation.result,
        )
    except Exception as exc:  # noqa: BLE001
        _completion(context, invocation, failed=True)
        raise _named_error(
            invocation.request.profile_id,
            invocation.request.runner_type,
            invocation.request.stage,
            exc,
            secrets=_request_sensitive_values(invocation.request),
        ) from exc
    _completion(context, invocation, failed=failed)
    if not failed:
        _print_normalized_output(context, invocation)
    return invocation.result


def run_runner_once(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
    *,
    working_directory: pathlib.Path,
) -> RunnerResult:
    """Resolve, invoke, validate, finalize, and report one runner call."""

    invocation = _invoke_runner(
        context,
        step,
        prompt,
        input_paths,
        stage_name,
        working_directory=working_directory,
    )
    return _finalize_invocation(context, invocation, failed=False)


__all__ = ["run_runner_once"]
