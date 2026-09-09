"""OpenCode adapter for the internal runner boundary.

The compatibility invocation deliberately remains the shipped ``opencode
run`` shape.  This module owns the adapter-level protocol and lifecycle
semantics; workflow retries and JSON/schema handling remain in orchestration.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any

from ..llm import (
    OPENCODE_APPLY_AGENT,
    OPENCODE_READ_ONLY_AGENT,
    _transcript_dir,
    build_opencode_security_config,
    non_agent_opencode_config,
    opencode_isolation_env,
    resolve_opencode_executable,
    sanitize_filename_component,
    validate_opencode_attachments,
)
from ...paths import resolve_contained_path, write_text_no_follow
from .contracts import (
    RunnerCapabilities,
    RunnerContractError,
    RunnerError,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
    bounded_diagnostic,
    require_final_text,
    require_zero_exit,
)
from .process import run_process


class OpenCodeRunner:
    """Run one isolated OpenCode invocation and optionally finalize its session."""

    capabilities = RunnerCapabilities(
        supports_resume=True,
        supports_finalize=True,
        supports_transcript=True,
    )

    def _context(self, request: RunnerRequest) -> Any:
        context = request.integration_context
        if context is None:
            raise RunnerContractError(
                "OpenCode runner requires an integration context for executable and isolation policy"
            )
        return context

    @staticmethod
    def _parse_output(raw: str, request: RunnerRequest) -> tuple[str | None, str]:
        session_id: str | None = None
        text_parts: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RunnerProtocolError(
                    "OpenCode emitted malformed JSONL output",
                    details="invalid event",
                ) from exc
            if not isinstance(event, dict):
                raise RunnerProtocolError("OpenCode emitted a non-object JSONL event")

            event_type = event.get("type")
            if event_type in {"error", "fatal", "session.error"} or (
                isinstance(event.get("error"), (str, dict, list))
                and event_type not in {"text", "step_finish"}
            ):
                # Do not serialize the provider event: OpenCode may echo the
                # instruction, artifact bodies, or credential-shaped data.
                raise RunnerProtocolError("OpenCode reported an error event")

            for key in ("sessionID", "session_id"):
                value = event.get(key)
                if session_id is None and isinstance(value, str) and value.strip():
                    session_id = value.strip()

            if event_type != "text":
                # New OpenCode event kinds are additive unless they are an
                # explicit error.  Only text parts form the final response.
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                raise RunnerProtocolError("OpenCode text event has no text part")
            text = part.get("text")
            if not isinstance(text, str):
                raise RunnerProtocolError("OpenCode text event has an invalid text value")
            text_parts.append(text)

        final_text = "\n".join(text_parts).strip()
        if not final_text:
            raise RunnerMissingOutputError(
                f"OpenCode produced no final response for {request.stage!r}"
            )
        return session_id, final_text

    @staticmethod
    def _session_id_from_partial_output(raw: str) -> str | None:
        """Recover only a complete session identifier from a bounded prefix."""

        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            for key in ("sessionID", "session_id"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _argv(
        self,
        request: RunnerRequest,
        context: Any,
        executable: str,
        attachments: list[pathlib.Path],
        *,
        prompt: str,
    ) -> list[str]:
        agent = OPENCODE_APPLY_AGENT if request.mode == "apply" else OPENCODE_READ_ONLY_AGENT
        config = getattr(getattr(context, "config", None), "llm", None)
        title_prefix = getattr(config, "session_title_prefix", "ai-push-hooks")
        argv = [
            executable,
            "run",
            "--agent",
            agent,
            "--pure",
            "--format",
            "json",
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if request.variant:
            argv.extend(["--variant", request.variant])
        if request.resume_session and request.session_id:
            argv.extend(["--session", request.session_id])
        else:
            argv.extend(
                [
                    "--title",
                    f"{title_prefix} {context.run_id} {request.stage}",
                ]
            )
        for path in attachments:
            argv.extend(["--file", str(path)])
        argv.extend(["--", prompt])
        return argv

    def run(self, request: RunnerRequest) -> RunnerResult:
        if request.runner_type != "opencode":
            raise RunnerContractError(
                f"OpenCode runner cannot handle runner type {request.runner_type!r}"
            )
        context = self._context(request)
        if request.mode == "apply" and not request.allow_paths:
            raise RunnerContractError("OpenCode apply requests require allow_paths")
        if request.mode == "apply" and request.project_access == "artifacts":
            # Apply always runs in a host-created staging projection, even for
            # the compatibility profile.
            working_directory = request.cwd
        elif request.project_access == "project":
            working_directory = request.cwd
        else:
            working_directory = None

        if working_directory is not None and not working_directory.is_dir():
            raise RunnerContractError("OpenCode request cwd must be an existing directory")

        raw_paths = [artifact.path for artifact in request.artifacts]
        attachments = validate_opencode_attachments(context, [path for path in raw_paths if path])
        agent = "apply" if request.mode == "apply" else "read-only"
        _agent_name, security_config = build_opencode_security_config(
            agent,
            request.allow_paths,
            non_vcs_working_directory=(
                working_directory if request.mode == "apply" else None
            ),
            project_read_root=(
                working_directory if request.project_access == "project" else None
            ),
        )
        executable = getattr(context, "opencode_executable", None) or resolve_opencode_executable()
        isolated_env = opencode_isolation_env(context, security_config, request.stage)
        # Normal workflow requests have hook-owned paths and retain the
        # shipped native --file transport.  A path-less contract request still
        # receives complete logical artifact content rather than silently
        # dropping it; this fallback is only used when native attachment paths
        # are unavailable.
        prompt = (
            request.prompt_packet().render()
            if any(path is None for path in raw_paths)
            else request.instruction
        )
        argv = self._argv(request, context, executable, attachments, prompt=prompt)

        def invoke(cwd: pathlib.Path) -> Any:
            completed = run_process(
                argv,
                cwd=cwd,
                timeout_seconds=request.timeout_seconds,
                env=isolated_env,
            )
            if getattr(completed, "stdout_truncated", False) or getattr(
                completed, "stderr_truncated", False
            ):
                error = RunnerProtocolError("OpenCode process output was truncated")
                # The neutral contract currently has no failure envelope.  A
                # best-effort, non-secret attribute lets ST-3 finalize a
                # session that was announced before output truncation without
                # treating the truncated response as valid.
                setattr(error, "session_id", self._session_id_from_partial_output(completed.stdout))
                raise error
            return completed

        if working_directory is None:
            with tempfile.TemporaryDirectory(prefix="ai-push-hooks-readonly-") as directory:
                completed = invoke(pathlib.Path(directory).resolve(strict=True))
        else:
            completed = invoke(working_directory.resolve(strict=True))

        result = RunnerResult(
            final_text="",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        # Check the process status before parsing.  A failed child may emit a
        # partial or malformed stream; the process failure is the truthful,
        # normalized terminal diagnosis.
        try:
            require_zero_exit(request, result, env=isolated_env)
        except RunnerError as error:
            # Preserve cleanup identity for a non-zero terminal process while
            # keeping stdout/stderr out of the exception object/message.
            setattr(error, "session_id", self._session_id_from_partial_output(completed.stdout))
            raise
        session_id, final_text = self._parse_output(completed.stdout, request)
        if request.resume_session and session_id is None:
            session_id = request.session_id
        return RunnerResult(
            final_text=require_final_text(final_text, mode=request.mode),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            session=(
                SessionMetadata(session_id=session_id, state="persisted", resumable=True)
                if session_id
                else SessionMetadata()
            ),
        )

    def _lifecycle_process(
        self,
        request: RunnerRequest,
        context: Any,
        argv: list[str],
        action: str,
    ) -> Any:
        # Export/delete must use the original stage isolation root.  Using a
        # different action-specific root makes retained sessions invisible to
        # OpenCode because its HOME/XDG state is isolated per stage.
        env = opencode_isolation_env(
            context,
            non_agent_opencode_config(),
            request.stage,
        )
        with tempfile.TemporaryDirectory(prefix=f"ai-push-hooks-session-{action}-") as directory:
            return run_process(
                argv,
                cwd=pathlib.Path(directory).resolve(strict=True),
                timeout_seconds=request.timeout_seconds,
                env=env,
            )

    def finalize(self, request: RunnerRequest, result: RunnerResult) -> RunnerResult:
        session = result.session
        if session is None or not session.session_id:
            return result
        context = self._context(request)
        session_id = session.session_id
        transcript_path: pathlib.Path | None = None

        if getattr(context.config.logging, "capture_llm_transcript", False):
            transcript_dir = _transcript_dir(context)
            if transcript_dir is not None:
                transcript_path = resolve_contained_path(
                    transcript_dir,
                    (
                        f"{sanitize_filename_component(context.run_id)}-"
                        f"{sanitize_filename_component(request.stage)}-"
                        f"{sanitize_filename_component(session_id)}.json"
                    ),
                    "OpenCode transcript path",
                )
                try:
                    executable = getattr(context, "opencode_executable", None) or resolve_opencode_executable()
                    exported = self._lifecycle_process(
                        request,
                        context,
                        [executable, "export", session_id, "--pure"],
                        "export",
                    )
                    if (
                        exported.returncode == 0
                        and not getattr(exported, "stdout_truncated", False)
                        and not getattr(exported, "stderr_truncated", False)
                        and exported.stdout.strip()
                    ):
                        write_text_no_follow(transcript_path, exported.stdout.strip() + "\n")
                    else:
                        transcript_path = None
                        self._warn_export(context, request, session_id, "export returned no transcript")
                except Exception as exc:  # noqa: BLE001
                    transcript_path = None
                    self._warn_export(context, request, session_id, type(exc).__name__)

        deleted = False
        if getattr(context.config.llm, "delete_session_after_run", False):
            try:
                executable = getattr(context, "opencode_executable", None) or resolve_opencode_executable()
                deleted_result = self._lifecycle_process(
                    request,
                    context,
                    [executable, "session", "delete", session_id, "--pure"],
                    "delete",
                )
                deleted = deleted_result.returncode == 0
                if not deleted:
                    self._warn_export(context, request, session_id, "session deletion failed")
            except Exception as exc:  # noqa: BLE001
                self._warn_export(context, request, session_id, f"delete {type(exc).__name__}")

        state = "deleted" if deleted else "persisted"
        finalized_session = SessionMetadata(
            session_id=session_id,
            state=state,
            resumable=not deleted,
            transcript=str(transcript_path) if transcript_path is not None else None,
        )
        return RunnerResult(
            final_text=result.final_text,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            session=finalized_session,
            transcript=result.transcript,
        )

    @staticmethod
    def _warn_export(context: Any, request: RunnerRequest, session_id: str, reason: str) -> None:
        logger = getattr(context, "logger", None)
        if logger is not None:
            logger.warn(
                "llm.transcript_export_failed",
                "Could not capture or delete the OpenCode session cleanly.",
                stage_name=request.stage,
                session_id=session_id,
                reason=bounded_diagnostic(reason, max_chars=120),
            )


def create_runner() -> OpenCodeRunner:
    """Registry factory for the built-in OpenCode adapter."""

    return OpenCodeRunner()
