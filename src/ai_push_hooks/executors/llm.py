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
from .exec import ensure_dir, extract_pr_url, resolve_storage_path, run_command

OPENCODE_READ_ONLY_AGENT = "ai-push-hooks-readonly"
OPENCODE_APPLY_AGENT = "ai-push-hooks-apply"
OPENCODE_AGENT_POLICIES = frozenset({"read-only", "apply"})
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
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
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
    completed = run_command(
        [
            context.opencode_executable or resolve_opencode_executable(),
            "export",
            session_id,
            "--pure",
        ],
        cwd=context.repo_root,
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


def delete_opencode_session(context: RuntimeContext, session_id: str) -> None:
    run_command(
        [
            context.opencode_executable or resolve_opencode_executable(),
            "session",
            "delete",
            session_id,
            "--pure",
        ],
        cwd=context.repo_root,
        timeout=context.config.llm.timeout_seconds,
        check=False,
        env=opencode_isolation_env(context, non_agent_opencode_config(), "session-delete"),
        inherit_env=False,
    )


def finalize_opencode_session(context: RuntimeContext, stage_name: str, session_id: str | None) -> None:
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
        export_opencode_session_json(context, session_id, export_path)
    if context.config.llm.delete_session_after_run:
        delete_opencode_session(context, session_id)


def build_opencode_security_config(
    agent_policy: str,
    allow_paths: tuple[str, ...] = (),
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
        edit_permissions = {
            "*": "deny",
            **{pattern: "allow" for pattern in sorted(permission_patterns)},
        }
        edit_permissions[".git"] = "deny"
        edit_permissions[".git/**"] = "deny"
        permissions["edit"] = edit_permissions
    else:
        raise HookError(f"Unsupported OpenCode agent policy: {agent_policy}")

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
) -> OpenCodeRunResult:
    if agent not in OPENCODE_AGENT_POLICIES:
        raise HookError(f"Unsupported OpenCode agent policy: {agent}")
    if agent == "apply" and not allow_paths:
        raise HookError("OpenCode apply agent requires an explicit non-empty allow_paths")
    if agent == "read-only" and allow_paths:
        raise HookError("OpenCode read-only agent does not accept write paths")

    validated_files = validate_opencode_attachments(context, files)
    agent_name, security_config = build_opencode_security_config(agent, allow_paths)
    executable = context.opencode_executable or resolve_opencode_executable()
    context.logger.llm_call(stage_name, purpose, context.config.llm.model, attempt, total_attempts)
    isolated_env = opencode_isolation_env(context, security_config, stage_name)
    cmd = [
        executable,
        "run",
        "--agent",
        agent_name,
        "--pure",
        "--format",
        "json",
        "--model",
        context.config.llm.model,
    ]
    if context.config.llm.variant:
        cmd.extend(["--variant", context.config.llm.variant])
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
            cwd=working_directory.resolve(strict=True),
            timeout=context.config.llm.timeout_seconds,
            check=False,
            env=isolated_env,
            inherit_env=False,
        )
    session_id, text_output = parse_opencode_json_run_output(completed.stdout or "")
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if context.config.logging.print_llm_output and stdout.strip():
        print(stdout)
    return OpenCodeRunResult(
        output_text=text_output if text_output else stdout.strip(),
        session_id=session_id or existing_session_id,
        stdout=stdout,
        stderr=stderr,
        return_code=completed.returncode,
    )


def run_llm_step(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> Any:
    total_attempts = context.config.llm.json_max_retries + 1
    session_id: str | None = None
    prompt_text = prompt
    last_error = ""
    last_output = ""
    wants_json = bool(step.schema)
    expects_json_array = step.schema in {"string_array", "docs_issue_array"}
    for attempt in range(1, total_attempts + 1):
        try:
            result = call_opencode(
                context,
                stage_name=stage_name,
                purpose=f"{step.type}:{step.id}",
                prompt=prompt_text,
                files=input_paths,
                agent="read-only",
                attempt=attempt,
                total_attempts=total_attempts,
                existing_session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            finalize_opencode_session(context, stage_name, session_id)
            raise
        session_id = result.session_id
        if result.return_code != 0:
            finalize_opencode_session(context, stage_name, session_id)
            details = result.stderr.strip() or result.stdout.strip() or f"exit code {result.return_code}"
            raise HookError(f"OpenCode command failed: {details}")
        try:
            if not wants_json:
                payload = result.output_text
            else:
                if expects_json_array:
                    payload = extract_json_array(result.output_text)
                else:
                    payload = extract_json_object(result.output_text)
                payload = validate_schema(step.schema, payload)
        except HookError as exc:
            last_error = str(exc)
            last_output = result.output_text
            if attempt >= total_attempts:
                finalize_opencode_session(context, stage_name, session_id)
                raise HookError(
                    f"Model failed to return valid JSON for {stage_name}: "
                    f"{last_error}. {last_output[:400]}"
                ) from exc
            snippet = last_output[: context.config.llm.invalid_json_feedback_max_chars]
            if expects_json_array:
                suffix = "Return ONLY valid JSON array."
            else:
                suffix = "Return ONLY valid JSON object."
            prompt_text = (
                prompt
                + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                + f"Parse error: {last_error}\n"
                + suffix
                + "\nPrevious invalid output:\n```text\n"
                + snippet
                + "\n```"
            )
            if context.config.llm.json_retry_new_session:
                finalize_opencode_session(context, stage_name, session_id)
                session_id = None
            pr_url = extract_pr_url(last_output)
            if pr_url:
                context.logger.info(
                    "llm.invalid_json_pr_url_hint",
                    "Detected PR URL in invalid JSON output",
                    stage_name=stage_name,
                    url=pr_url,
                )
            continue
        finalize_opencode_session(context, stage_name, session_id)
        return payload
    raise HookError(f"Model failed to return valid JSON for {stage_name}")  # pragma: no cover
