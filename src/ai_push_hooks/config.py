from __future__ import annotations

import os
import pathlib
import stat
from typing import Any

from .executors.exec import env_bool, resolve_git_common_dir, resolve_git_dir
from .paths import (
    is_path_within,
    normalized_component,
    path_has_symlink,
    relative_path_parts,
    resolve_contained_path,
    validate_path_component,
)
from .prompts_builtin import BUILTIN_PROMPTS
from .types import GeneralConfig, HookConfig, HookError, LlmConfig, LoggingConfig, ModuleConfig, StepConfig, SUPPORTED_STEP_TYPES, WorkflowConfig

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ALLOWED_TOP_LEVEL_KEYS = {"general", "llm", "logging", "workflow", "modules"}
STORAGE_NAMESPACE_PARTS = (".git", "ai-push-hooks")


def _normalize_step(raw: dict[str, Any]) -> StepConfig:
    step_type = str(raw.get("type", "")).strip()
    if step_type not in SUPPORTED_STEP_TYPES:
        raise HookError(f"Unknown step type: {step_type}")
    step = StepConfig(
        id=str(raw.get("id", "")).strip(),
        type=step_type,
        inputs=tuple(str(item) for item in raw.get("inputs", []) or []),
        output=str(raw.get("output")).strip() if raw.get("output") is not None else None,
        schema=str(raw.get("schema")).strip() if raw.get("schema") is not None else None,
        prompt=str(raw.get("prompt")).strip() if raw.get("prompt") is not None else None,
        prompt_file=str(raw.get("prompt_file")).strip() if raw.get("prompt_file") is not None else None,
        fallback_prompt_id=(
            str(raw.get("fallback_prompt_id")).strip()
            if raw.get("fallback_prompt_id") is not None
            else None
        ),
        collector=str(raw.get("collector")).strip() if raw.get("collector") is not None else None,
        allow_paths=tuple(str(item) for item in raw.get("allow_paths", []) or []),
        executor=str(raw.get("executor")).strip() if raw.get("executor") is not None else None,
        assertion=str(raw.get("assertion")).strip() if raw.get("assertion") is not None else None,
        when_env=str(raw.get("when_env")).strip() if raw.get("when_env") is not None else None,
    )
    if not step.id:
        raise HookError("Every workflow step requires a non-empty id")
    validate_path_component(step.id, "Workflow step id")
    if step.output:
        validate_path_component(step.output, f"Output for step `{step.id}`")
    for pattern in step.allow_paths:
        parts = relative_path_parts(pattern, f"allow_paths entry for step `{step.id}`")
        if any(normalized_component(part) == ".git" for part in parts):
            raise HookError(f"Apply step `{step.id}` may not allow Git metadata paths")
        if normalized_component(parts[-1]) == "agents.md":
            raise HookError(f"Apply step `{step.id}` may not allow AGENTS.md")
    if step.is_promptable and not any([step.prompt, step.prompt_file, step.fallback_prompt_id]):
        raise HookError(f"Promptable step `{step.id}` requires prompt, prompt_file, or fallback_prompt_id")
    if step.type == "collect" and not step.collector:
        raise HookError(f"Collect step `{step.id}` requires collector")
    if step.type == "llm" and not step.output:
        raise HookError(f"LLM step `{step.id}` requires output")
    if step.type == "apply" and not step.allow_paths:
        raise HookError(f"Apply step `{step.id}` requires allow_paths")
    if step.type == "exec" and not step.executor:
        raise HookError(f"Exec step `{step.id}` requires executor")
    if step.type == "assert" and not step.assertion:
        raise HookError(f"Assert step `{step.id}` requires assertion")
    return step


def _build_config(raw: dict[str, Any]) -> HookConfig:
    unknown = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise HookError(
            "Legacy or unsupported config keys are not allowed: " + ", ".join(sorted(unknown))
        )

    workflow_modules = tuple(str(item) for item in raw.get("workflow", {}).get("modules", []) or [])
    if not workflow_modules:
        raise HookError("workflow.modules must define at least one module id")

    module_payload = raw.get("modules", {})
    if not isinstance(module_payload, dict):
        raise HookError("modules must be a table")

    modules: dict[str, ModuleConfig] = {}
    for module_id in workflow_modules:
        validate_path_component(module_id, "Workflow module id")
        if module_id not in module_payload:
            raise HookError(f"workflow.modules references unknown module `{module_id}`")
        module_raw = module_payload[module_id]
        steps_raw = module_raw.get("steps", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            raise HookError(f"Module `{module_id}` must define a non-empty steps array")
        modules[module_id] = ModuleConfig(
            id=module_id,
            enabled=bool(module_raw.get("enabled", True)),
            steps=tuple(_normalize_step(step) for step in steps_raw),
        )

    general = GeneralConfig(**raw.get("general", {}))
    llm = LlmConfig(**raw.get("llm", {}))
    logging = LoggingConfig(**raw.get("logging", {}))
    for label, storage_path in (
        ("logging.dir", logging.dir),
        ("logging.transcript_dir", logging.transcript_dir),
        ("logging.summary_dir", logging.summary_dir),
    ):
        parts = relative_path_parts(storage_path, label)
        if parts[:2] != STORAGE_NAMESPACE_PARTS or len(parts) < 3:
            raise HookError(f"{label} must be inside .git/ai-push-hooks/")
    return HookConfig(
        general=general,
        llm=llm,
        logging=logging,
        workflow=WorkflowConfig(modules=workflow_modules),
        modules=modules,
    )


def _apply_env_overrides(config: HookConfig) -> HookConfig:
    raw = {
        "general": {
            "enabled": config.general.enabled,
            "allow_push_on_error": config.general.allow_push_on_error,
            "require_clean_worktree": config.general.require_clean_worktree,
            "skip_on_sync_branch": config.general.skip_on_sync_branch,
            "base_branch": config.general.base_branch,
        },
        "llm": config.llm.__dict__.copy(),
        "logging": config.logging.__dict__.copy(),
        "workflow": {"modules": list(config.workflow.modules)},
        "modules": {},
    }
    for module_id, module in config.modules.items():
        raw["modules"][module_id] = {
            "enabled": module.enabled,
            "steps": [step.__dict__.copy() for step in module.steps],
        }

    skip = env_bool("AI_PUSH_HOOKS_SKIP")
    if skip is True:
        raw["general"]["enabled"] = False
    allow_on_error = env_bool("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR")
    if allow_on_error is not None:
        raw["general"]["allow_push_on_error"] = allow_on_error
    require_clean = env_bool("AI_PUSH_HOOKS_REQUIRE_CLEAN")
    if require_clean is not None:
        raw["general"]["require_clean_worktree"] = require_clean
    allow_dirty = env_bool("AI_PUSH_HOOKS_ALLOW_DIRTY")
    if allow_dirty is True:
        raw["general"]["require_clean_worktree"] = False
    base_branch = os.getenv("AI_PUSH_HOOKS_BASE_BRANCH")
    if base_branch:
        raw["general"]["base_branch"] = base_branch.strip() or "main"

    logging_level = os.getenv("AI_PUSH_HOOKS_LOG_LEVEL")
    if logging_level:
        raw["logging"]["level"] = logging_level.strip().lower()
    print_output = env_bool("AI_PUSH_HOOKS_PRINT_LLM_OUTPUT")
    if print_output is not None:
        raw["logging"]["print_llm_output"] = print_output
    model = os.getenv("AI_PUSH_HOOKS_MODEL")
    if model:
        raw["llm"]["model"] = model
    variant = os.getenv("AI_PUSH_HOOKS_VARIANT")
    if variant is not None:
        raw["llm"]["variant"] = variant.strip()
    timeout = os.getenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS")
    if timeout:
        raw["llm"]["timeout_seconds"] = int(timeout)
    return _build_config(raw)


def load_config(repo_root: pathlib.Path) -> tuple[HookConfig, pathlib.Path]:
    config_path = repo_root / "ai-push-hooks.toml"
    if not config_path.exists():
        raise HookError(
            "Missing required config file `ai-push-hooks.toml` in repo root. "
            "Run `ai-push-hooks init --template minimal-docs` first"
        )
    text = config_path.read_text(encoding="utf-8")
    loaded = tomllib.loads(text)
    if not isinstance(loaded, dict):
        raise HookError(f"Invalid config format in {config_path}")
    return _apply_env_overrides(_build_config(loaded)), config_path


def resolve_prompt_text(repo_root: pathlib.Path, step: StepConfig) -> str:
    if step.prompt and step.prompt.strip():
        return step.prompt.strip()
    if step.prompt_file:
        parts = relative_path_parts(step.prompt_file, f"Prompt file for step `{step.id}`")
        if any(normalized_component(part) == ".git" for part in parts):
            raise HookError(f"Prompt file for step `{step.id}` must not reference Git metadata")
        lexical_prompt_path = repo_root.joinpath(*parts)
        if path_has_symlink(repo_root, lexical_prompt_path):
            raise HookError(f"Prompt file for step `{step.id}` must not traverse a symlink")
        prompt_path = resolve_contained_path(
            repo_root,
            step.prompt_file,
            f"Prompt file for step `{step.id}`",
        )
        resolved_prompt_path = prompt_path.resolve(strict=False)
        try:
            git_roots = (
                resolve_git_dir(repo_root).resolve(strict=True),
                resolve_git_common_dir(repo_root).resolve(strict=True),
            )
        except HookError:
            git_roots = ()
        if any(is_path_within(resolved_prompt_path, git_root) for git_root in git_roots):
            raise HookError(f"Prompt file for step `{step.id}` must not resolve inside Git metadata")
        if prompt_path.exists():
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(prompt_path, flags)
            except OSError as exc:
                raise HookError(
                    f"Prompt file could not be opened safely for step `{step.id}`: {prompt_path}"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise HookError(
                        f"Prompt file is not a regular file for step `{step.id}`: {prompt_path}"
                    )
                with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                    descriptor = -1
                    text = handle.read().strip()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if text:
                return text
        if step.fallback_prompt_id:
            return resolve_builtin_prompt(step.fallback_prompt_id)
        raise HookError(f"Prompt file not found or empty for step `{step.id}`: {prompt_path}")
    if step.fallback_prompt_id:
        return resolve_builtin_prompt(step.fallback_prompt_id)
    raise HookError(f"No prompt source available for step `{step.id}`")


def resolve_builtin_prompt(prompt_id: str) -> str:
    prompt = BUILTIN_PROMPTS.get(prompt_id)
    if not prompt:
        raise HookError(f"Unknown built-in prompt id: {prompt_id}")
    return prompt
