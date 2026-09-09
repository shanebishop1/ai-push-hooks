from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any

from ..paths import (
    ensure_private_directory,
    is_path_within,
    path_has_symlink,
    resolve_contained_path,
    write_text_no_follow,
)
from ..types import HookError, RuntimeContext, StepConfig
from .exec import ensure_dir, resolve_storage_path, run_command

OPENCODE_READ_ONLY_AGENT = "ai-push-hooks-readonly"
OPENCODE_APPLY_AGENT = "ai-push-hooks-apply"
OPENCODE_AGENT_POLICIES = frozenset({"read-only", "apply"})
# A sentinel keeps the legacy helper's implicit flat-config behavior while
# allowing the runner boundary to explicitly pass ``None`` and omit optional
# OpenCode flags.
_DEFAULT_MODEL = object()
_DEFAULT_VARIANT = object()
PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "COHERE_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "GROQ_",
    "MISTRAL_",
    "OPENAI_",
    "OPENROUTER_",
    "VERTEX_",
    "XAI_",
)
SAFE_PROCESS_ENV_NAMES = frozenset(
    {
        "PATH",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",
    }
)


def _actual_xdg_data_home() -> pathlib.Path:
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    if configured:
        return pathlib.Path(configured).expanduser().resolve(strict=False)
    return (pathlib.Path.home() / ".local" / "share").resolve(strict=False)


def opencode_isolation_env(
    context: RuntimeContext,
    security_config: dict[str, Any],
    stage_name: str,
) -> dict[str, str | None]:
    lexical_isolation_root = (
        context.run_dir / "opencode-isolation" / sanitize_filename_component(stage_name)
    )
    if path_has_symlink(context.run_dir, lexical_isolation_root):
        raise HookError(f"OpenCode isolation directory must not traverse a symlink: {stage_name}")
    isolation_root = resolve_contained_path(
        context.run_dir,
        f"opencode-isolation/{sanitize_filename_component(stage_name)}",
        "OpenCode isolation directory",
    )
    home = isolation_root / "home"
    config_home = isolation_root / "config"
    cache_home = isolation_root / "cache"
    state_home = isolation_root / "state"
    ensure_private_directory(isolation_root)
    for path in (home, config_home, cache_home, state_home):
        ensure_private_directory(path, private_root=isolation_root)

    isolated: dict[str, str | None] = {
        name: value
        for name, value in os.environ.items()
        if name in SAFE_PROCESS_ENV_NAMES or name.startswith(PROVIDER_ENV_PREFIXES)
    }
    isolated.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_CACHE_HOME": str(cache_home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_DATA_HOME": str(_actual_xdg_data_home()),
        "OPENCODE_CONFIG_CONTENT": json.dumps(security_config, ensure_ascii=True),
        "OPENCODE_CONFIG_DIR": str(config_home),
        "OPENCODE_PURE": "true",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "true",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
        "OPENCODE_DISABLE_SHARE": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
    })
    return isolated


def non_agent_opencode_config() -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "mcp": {},
        "share": "disabled",
        "instructions": [],
        "formatter": False,
        "lsp": False,
        "command": {},
        "permission": {"*": "deny"},
    }


@dataclass
class OpenCodeRunResult:
    output_text: str
    session_id: str | None
    stdout: str
    stderr: str
    return_code: int


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "value"


def resolve_opencode_executable() -> str:
    opencode_path = shutil.which("opencode")
    if opencode_path:
        return opencode_path
    cli_path = shutil.which("opencode-cli")
    if cli_path:
        return cli_path
    raise HookError("opencode is required but not installed")


def parse_opencode_json_run_output(raw: str) -> tuple[str | None, str]:
    session_id: str | None = None
    parts: list[str] = []
    for line in raw.splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if session_id is None and isinstance(event.get("sessionID"), str):
            session_id = str(event["sessionID"]).strip()
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
            parts.append(part["text"])
    return session_id, "\n".join(parts).strip()


def extract_json_array(text: str) -> list[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise HookError("Could not find JSON array in model output")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HookError(f"Failed to parse JSON array from model output: {exc}") from exc
    if not isinstance(payload, list):
        raise HookError("Model output JSON is not an array")
    return payload


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise HookError("Could not find JSON object in model output")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HookError(f"Failed to parse JSON object from model output: {exc}") from exc
    if not isinstance(payload, dict):
        raise HookError("Model output JSON is not an object")
    return payload


def validate_schema(schema: str | None, payload: Any) -> Any:
    if schema is None:
        return payload
    if schema == "string_array":
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise HookError("Expected schema string_array")
        return payload
    if schema == "docs_issue_array":
        if not isinstance(payload, list):
            raise HookError("Expected schema docs_issue_array")
        for item in payload:
            if not isinstance(item, dict):
                raise HookError("docs_issue_array items must be objects")
            if not str(item.get("file", "")).strip() or not str(item.get("description", "")).strip():
                raise HookError("docs_issue_array items require file and description")
        return payload
    if schema == "beads_alignment_result":
        if not isinstance(payload, dict):
            raise HookError("Expected schema beads_alignment_result")
        commands = payload.get("commands", [])
        if commands is not None and (
            not isinstance(commands, list) or not all(isinstance(item, str) for item in commands)
        ):
            raise HookError("beads_alignment_result.commands must be an array of strings")
        return payload
    if schema == "pr_create_payload":
        if not isinstance(payload, dict):
            raise HookError("Expected schema pr_create_payload")
        return payload
    raise HookError(f"Unsupported schema: {schema}")


def _transcript_dir(context: RuntimeContext) -> pathlib.Path | None:
    if not context.config.logging.capture_llm_transcript:
        return None
    return ensure_dir(
        resolve_storage_path(
            context.repo_root,
            context.git_dir,
            context.config.logging.transcript_dir,
        )
    )


def export_opencode_session_json(
    context: RuntimeContext,
    session_id: str,
    export_path: pathlib.Path,
) -> bool:
    with tempfile.TemporaryDirectory(
        prefix="ai-push-hooks-session-export-"
    ) as temporary_directory:
        completed = run_command(
            [
                context.opencode_executable or resolve_opencode_executable(),
                "export",
                session_id,
                "--pure",
            ],
            cwd=pathlib.Path(temporary_directory).resolve(strict=True),
            timeout=context.config.llm.timeout_seconds,
            check=False,
            env=opencode_isolation_env(context, non_agent_opencode_config(), "session-export"),
            inherit_env=False,
        )
    if completed.returncode != 0:
        return False
    payload = (completed.stdout or "").strip()
    if not payload:
        return False
    write_text_no_follow(export_path, payload + "\n")
    return True


def delete_opencode_session(context: RuntimeContext, session_id: str) -> bool:
    with tempfile.TemporaryDirectory(
        prefix="ai-push-hooks-session-delete-"
    ) as temporary_directory:
        completed = run_command(
            [
                context.opencode_executable or resolve_opencode_executable(),
                "session",
                "delete",
                session_id,
                "--pure",
            ],
            cwd=pathlib.Path(temporary_directory).resolve(strict=True),
            timeout=context.config.llm.timeout_seconds,
            check=False,
            env=opencode_isolation_env(context, non_agent_opencode_config(), "session-delete"),
            inherit_env=False,
        )
    return completed.returncode == 0


def finalize_opencode_session(context: RuntimeContext, stage_name: str, session_id: str | None) -> None:
    """Capture and finalize a session without retaining it on export failure.

    Transcript capture is best effort. A failed or interrupted export emits a
    visible warning, then the configured deletion policy still runs so a
    failed capture does not silently retain provider data.
    """
    if not session_id:
        return
    transcript_dir = _transcript_dir(context)
    if transcript_dir is not None:
        export_name = (
            f"{sanitize_filename_component(context.run_id)}-"
            f"{sanitize_filename_component(stage_name)}-"
            f"{sanitize_filename_component(session_id)}.json"
        )
        export_path = resolve_contained_path(
            transcript_dir,
            export_name,
            "OpenCode transcript path",
        )
        export_failure: str | None = None
        try:
            exported = export_opencode_session_json(context, session_id, export_path)
        except Exception as exc:  # noqa: BLE001
            exported = False
            export_failure = type(exc).__name__
        if not exported:
            context.logger.warn(
                "llm.transcript_export_failed",
                "Could not capture the OpenCode transcript; applying configured session deletion.",
                stage_name=stage_name,
                session_id=session_id,
                reason=export_failure or "export returned no transcript",
            )
    if context.config.llm.delete_session_after_run:
        delete_opencode_session(context, session_id)


def build_opencode_security_config(
    agent_policy: str,
    allow_paths: tuple[str, ...] = (),
    *,
    non_vcs_working_directory: pathlib.Path | None = None,
    project_read_root: pathlib.Path | None = None,
) -> tuple[str, dict[str, Any]]:
    permissions: dict[str, Any] = {
        "*": "deny",
        "read": "deny",
        "glob": "deny",
        "grep": "deny",
        "list": "deny",
        "edit": "deny",
        "bash": "deny",
        "task": "deny",
        "external_directory": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "lsp": "deny",
        "skill": "deny",
        "todowrite": "deny",
        "question": "deny",
        "doom_loop": "deny",
    }
    if agent_policy == "read-only":
        agent_name = OPENCODE_READ_ONLY_AGENT
        description = "Read-only ai-push-hooks analysis agent"
    elif agent_policy == "apply":
        if not allow_paths:
            raise HookError("OpenCode apply agent requires an explicit non-empty allow_paths")
        agent_name = OPENCODE_APPLY_AGENT
        description = "Path-restricted ai-push-hooks apply agent"
        permissions["read"] = "allow"
        permission_patterns: set[str] = set()
        for pattern in allow_paths:
            permission_patterns.add(pattern)
            collapsed = pattern
            while "**/" in collapsed:
                collapsed = collapsed.replace("**/", "", 1)
                permission_patterns.add(collapsed)
        protected_patterns = {".git", ".git/**"}
        if non_vcs_working_directory is not None:
            # OpenCode 1.18.29 assigns `/` as the worktree for a directory
            # without VCS metadata. Its write and edit tools then request the
            # `edit` permission using paths relative to that filesystem root,
            # not relative to the process cwd. Keep the staging checkout free
            # of Git metadata and qualify only its allowlisted paths.
            anchor = pathlib.Path(non_vcs_working_directory.anchor)
            prefix = non_vcs_working_directory.relative_to(anchor).as_posix()
            permission_patterns = {
                f"{prefix}/{pattern}" if prefix else pattern
                for pattern in permission_patterns
            }
            protected_patterns = {
                f"{prefix}/{pattern}" if prefix else pattern
                for pattern in protected_patterns
            }
        edit_permissions = {
            "*": "deny",
            **{pattern: "allow" for pattern in sorted(permission_patterns)},
        }
        for pattern in sorted(protected_patterns):
            edit_permissions[pattern] = "deny"
        permissions["edit"] = edit_permissions
    else:
        raise HookError(f"Unsupported OpenCode agent policy: {agent_policy}")

    if project_read_root is not None:
        # OpenCode's file permission matcher uses paths relative to its
        # filesystem worktree.  A real Git checkout is already rooted at the
        # requested worktree, so absolute filesystem-root-relative patterns
        # would never match README.md-style tool paths.  A non-VCS projection
        # is treated as a filesystem worktree rooted at `/`, so qualify those
        # patterns with the projection's absolute prefix.
        root = project_read_root.resolve(strict=False)
        if (root / ".git").exists():
            rooted_permissions: str | dict[str, str] = "allow"
        else:
            anchor = pathlib.Path(root.anchor)
            prefix = root.relative_to(anchor).as_posix()
            rooted_permissions = {
                "*": "deny",
                prefix: "allow",
                f"{prefix}/**": "allow",
            }
        for tool in ("read", "list", "glob", "grep"):
            permissions[tool] = (
                dict(rooted_permissions)
                if isinstance(rooted_permissions, dict)
                else rooted_permissions
            )

    return agent_name, {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "mcp": {},
        "share": "disabled",
        "instructions": [],
        "formatter": False,
        "lsp": False,
        "command": {},
        "agent": {
            agent_name: {
                "mode": "primary",
                "description": description,
                "permission": permissions,
            }
        },
    }


def validate_opencode_attachments(
    context: RuntimeContext,
    files: list[pathlib.Path],
) -> list[pathlib.Path]:
    run_root = context.run_dir.resolve(strict=True)
    validated: list[pathlib.Path] = []
    for file_path in files:
        lexical_path = pathlib.Path(os.path.abspath(file_path))
        if not is_path_within(lexical_path, run_root):
            raise HookError(f"OpenCode attachment is not a hook-owned artifact: {file_path}")
        if path_has_symlink(run_root, lexical_path):
            raise HookError(f"OpenCode attachment must not traverse a symlink: {file_path}")
        resolved_path = lexical_path.resolve(strict=True)
        if not is_path_within(resolved_path, run_root) or not resolved_path.is_file():
            raise HookError(f"OpenCode attachment must be a regular hook-owned file: {file_path}")
        validated.append(resolved_path)
    return validated


def call_opencode(
    context: RuntimeContext,
    stage_name: str,
    purpose: str,
    prompt: str,
    files: list[pathlib.Path],
    *,
    agent: str,
    allow_paths: tuple[str, ...] = (),
    working_directory: pathlib.Path | None = None,
    attempt: int | None = None,
    total_attempts: int | None = None,
    existing_session_id: str | None = None,
    model: str | None | object = _DEFAULT_MODEL,
    variant: str | None | object = _DEFAULT_VARIANT,
    project_access: str = "artifacts",
) -> OpenCodeRunResult:
    """Run OpenCode with a policy-specific isolated working directory.

    Apply callers must provide the hook-owned, non-VCS staging directory built
    by ``run_apply_step``; this is an internal precondition rather than a
    general repository-working-directory interface.
    """
    if agent not in OPENCODE_AGENT_POLICIES:
        raise HookError(f"Unsupported OpenCode agent policy: {agent}")
    if agent == "apply" and not allow_paths:
        raise HookError("OpenCode apply agent requires an explicit non-empty allow_paths")
    if agent == "apply" and working_directory is None:
        raise HookError("OpenCode apply agent requires an isolated staging directory")
    if agent == "read-only" and allow_paths:
        raise HookError("OpenCode read-only agent does not accept write paths")
    if project_access not in {"artifacts", "project"}:
        raise HookError(f"Unsupported OpenCode project access: {project_access}")
    if project_access == "project" and working_directory is None:
        raise HookError("OpenCode project access requires an explicit working directory")

    validated_files = validate_opencode_attachments(context, files)
    if working_directory is None:
        resolved_working_directory = None
    else:
        try:
            resolved_working_directory = working_directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise HookError(
                f"OpenCode working directory must be an existing directory: {working_directory}"
            ) from exc
        if not resolved_working_directory.is_dir():
            raise HookError(
                f"OpenCode working directory must be an existing directory: {working_directory}"
            )
    agent_name, security_config = build_opencode_security_config(
        agent,
        allow_paths,
        non_vcs_working_directory=(
            resolved_working_directory if agent == "apply" else None
        ),
        project_read_root=(
            resolved_working_directory if project_access == "project" else None
        ),
    )
    executable = context.opencode_executable or resolve_opencode_executable()
    effective_model = (
        context.config.llm.model if model is _DEFAULT_MODEL else model
    )
    effective_variant = (
        context.config.llm.variant if variant is _DEFAULT_VARIANT else variant
    )
    context.logger.llm_call(stage_name, purpose, effective_model or "", attempt, total_attempts)
    isolated_env = opencode_isolation_env(context, security_config, stage_name)
    cmd = [
        executable,
        "run",
        "--agent",
        agent_name,
        "--pure",
        "--format",
        "json",
    ]
    if effective_model:
        cmd.extend(["--model", effective_model])
    if effective_variant:
        cmd.extend(["--variant", effective_variant])
    if existing_session_id:
        cmd.extend(["--session", existing_session_id])
    else:
        cmd.extend(["--title", f"{context.config.llm.session_title_prefix} {context.run_id} {stage_name}"])
    for file_path in validated_files:
        cmd.extend(["--file", str(file_path)])
    cmd.extend(["--", prompt])

    if working_directory is None:
        with tempfile.TemporaryDirectory(prefix="ai-push-hooks-readonly-") as temporary_directory:
            completed = run_command(
                cmd,
                cwd=pathlib.Path(temporary_directory).resolve(strict=True),
                timeout=context.config.llm.timeout_seconds,
                check=False,
                env=isolated_env,
                inherit_env=False,
            )
    else:
        completed = run_command(
            cmd,
            cwd=resolved_working_directory,
            timeout=context.config.llm.timeout_seconds,
            check=False,
            env=isolated_env,
            inherit_env=False,
        )
    session_id, text_output = parse_opencode_json_run_output(completed.stdout or "")
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if context.config.logging.print_llm_output and text_output:
        # Print normalized assistant text only.  OpenCode's JSONL stream can
        # contain provider diagnostics and tool payloads which must not become
        # an accidental credential/prompt log.
        print(text_output)
    return OpenCodeRunResult(
        output_text=text_output if text_output else stdout.strip(),
        session_id=session_id or existing_session_id,
        stdout=stdout,
        stderr=stderr,
        return_code=completed.returncode,
    )


def _safe_invalid_output(invocation: Any, output: str) -> str:
    from .runners.contracts import request_sensitive_diagnostics

    request = invocation.request
    return request_sensitive_diagnostics(request, output, max_chars=400, env=os.environ)


def run_llm_step(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> Any:
    from .runner_workflow import _finalize_invocation, _invoke_runner, _named_error
    from ..config import resolve_runner_profile

    total_attempts = context.config.llm.json_max_retries + 1
    prompt_text = prompt
    last_error = ""
    last_output = ""
    wants_json = bool(step.schema)
    expects_json_array = step.schema in {"string_array", "docs_issue_array"}
    session_id: str | None = None
    resume_session = False
    selected_profile = step.runner or context.config.llm.runner
    try:
        profile = resolve_runner_profile(context.config, step)
    except Exception as exc:  # noqa: BLE001
        raise _named_error(selected_profile, "unknown", stage_name, exc) from exc
    retained_invocation = None

    # Artifact-only analysis must retain its historical empty scratch cwd;
    # project-aware analysis gets the actual checkout root so the adapter can
    # provide the selected runner's project-read behavior.
    if profile.project_access == "project":
        working_directory = context.repo_root.resolve(strict=True)
        temporary_directory = None
    else:
        temporary_directory = tempfile.TemporaryDirectory(prefix="ai-push-hooks-llm-")
        working_directory = pathlib.Path(temporary_directory.name).resolve(strict=True)

    try:
        for attempt in range(1, total_attempts + 1):
            invocation = _invoke_runner(
                context,
                step,
                prompt_text,
                input_paths,
                stage_name,
                working_directory=working_directory,
                session_id=session_id,
                resume_session=resume_session,
                attempt=attempt,
                total_attempts=total_attempts,
                prior_invocation=retained_invocation,
            )
            retained_invocation = None
            result = invocation.result
            try:
                if not wants_json:
                    payload = result.final_text
                else:
                    if expects_json_array:
                        payload = extract_json_array(result.final_text)
                    else:
                        payload = extract_json_object(result.final_text)
                    payload = validate_schema(step.schema, payload)
            except HookError as exc:
                last_error = str(exc)
                last_output = result.final_text
                if attempt >= total_attempts:
                    _finalize_invocation(context, invocation, failed=True)
                    safe_error = _safe_invalid_output(invocation, last_error)
                    raise HookError(
                        f"Runner profile `{profile.name}` ({profile.type}) failed at stage "
                        f"`{stage_name}`: invalid JSON: {safe_error}. "
                        f"{_safe_invalid_output(invocation, last_output)}"
                    ) from exc

                snippet = last_output[: context.config.llm.invalid_json_feedback_max_chars]
                suffix = (
                    "Return ONLY valid JSON array."
                    if expects_json_array
                    else "Return ONLY valid JSON object."
                )
                prompt_text = (
                    prompt
                    + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                    + f"Parse error: {last_error}\n"
                    + suffix
                    + "\nPrevious invalid output:\n```text\n"
                    + snippet
                    + "\n```"
                )

                session = result.session
                can_resume = bool(
                    getattr(getattr(invocation.runner, "capabilities", None), "supports_resume", False)
                    and session is not None
                    and session.session_id
                    and session.resumable
                )
                if context.config.llm.json_retry_new_session or not can_resume:
                    if not session or not session.session_id:
                        retry_reason = "session absent"
                    elif context.config.llm.json_retry_new_session:
                        retry_reason = "fresh session configured"
                    else:
                        retry_reason = "runner does not support resume"
                    retry_message = "Retrying with a fresh runner invocation."
                    if retry_reason == "runner does not support resume":
                        retry_message = (
                            "Retrying with a fresh runner invocation; unsupported session reuse."
                        )
                    elif retry_reason == "session absent":
                        retry_message = (
                            "Retrying with a fresh runner invocation; no reusable session was captured."
                        )
                    context.logger.status(
                        "llm.retry_fresh_session",
                        retry_message,
                        stage_name=stage_name,
                        runner_profile=profile.name,
                        runner_type=profile.type,
                        reason=retry_reason,
                    )
                    _finalize_invocation(context, invocation, failed=True)
                    session_id = None
                    resume_session = False
                else:
                    # Keep the exact captured session ID.  Do not invent a
                    # provider-specific resume command for completion output.
                    session_id = session.session_id
                    resume_session = True
                    retained_invocation = invocation
                    # The invocation itself completed, although its response
                    # failed downstream validation; report truthful metadata
                    # before reusing the retained session.
                    from .runner_workflow import _completion

                    _completion(context, invocation, failed=True)
                continue

            _finalize_invocation(context, invocation, failed=False)
            return payload
        raise HookError(
            f"Runner profile `{profile.name}` ({profile.type}) failed at stage `{stage_name}`: "
            "model did not return a valid result"
        )  # pragma: no cover
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
