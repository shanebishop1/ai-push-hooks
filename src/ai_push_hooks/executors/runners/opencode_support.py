"""Shared OpenCode isolation and hook-owned artifact policy helpers.

This module contains only the OpenCode-specific filesystem, environment, and
permission policy used by the active adapter.  Workflow orchestration remains
runner-neutral and the adapter owns invocation and session lifecycle.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
from typing import Any

from ...paths import (
    ensure_private_directory,
    is_path_within,
    path_has_symlink,
    resolve_contained_path,
)
from ...git_utils import ensure_dir, resolve_storage_path
from ...types import HookError, RuntimeContext


OPENCODE_READ_ONLY_AGENT = "ai-push-hooks-readonly"
OPENCODE_APPLY_AGENT = "ai-push-hooks-apply"
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
    isolated.update(
        {
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
        }
    )
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
            # OpenCode assigns `/` as the worktree for a directory without VCS
            # metadata, so qualify allowlisted paths with that filesystem root.
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


def validate_hook_owned_artifacts(
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
