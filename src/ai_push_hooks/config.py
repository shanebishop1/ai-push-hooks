from __future__ import annotations

import os
import pathlib
import re
import stat
from collections.abc import Mapping
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
from .types import (
    GeneralConfig,
    HookConfig,
    HookError,
    LlmConfig,
    LoggingConfig,
    ModuleConfig,
    RunnerProfile,
    StepConfig,
    SUPPORTED_STEP_TYPES,
    WorkflowConfig,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ALLOWED_TOP_LEVEL_KEYS = {"general", "llm", "logging", "workflow", "modules", "runners"}
STORAGE_NAMESPACE_PARTS = (".git", "ai-push-hooks")
GENERAL_KEYS = {
    "enabled",
    "allow_push_on_error",
    "require_clean_worktree",
    "skip_on_sync_branch",
    "base_branch",
}
LLM_KEYS = {
    "runner",
    "model",
    "variant",
    "timeout_seconds",
    "max_parallel",
    "json_max_retries",
    "invalid_json_feedback_max_chars",
    "json_retry_new_session",
    "delete_session_after_run",
    "max_diff_bytes",
    "session_title_prefix",
}
LOGGING_KEYS = {
    "level",
    "jsonl",
    "dir",
    "capture_llm_transcript",
    "transcript_dir",
    "summary_dir",
    "print_llm_output",
}
STEP_KEYS = {
    "id",
    "type",
    "inputs",
    "output",
    "schema",
    "prompt",
    "prompt_file",
    "fallback_prompt_id",
    "collector",
    "allow_paths",
    "executor",
    "assertion",
    "when_env",
    "runner",
}
RUNNER_TYPES = frozenset({"opencode", "codex", "claude", "command"})
PROJECT_ACCESS_VALUES = frozenset({"artifacts", "project"})
PROMPT_TRANSPORT_VALUES = frozenset({"stdin", "argv"})
RUNNER_KEYS = {
    "type",
    "model",
    "variant",
    "project_access",
    "command",
    "prompt_transport",
}
RUNNER_PLACEHOLDERS = frozenset({"{prompt}", "{model}", "{cwd}", "{stage}"})
RUNNER_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]*\}")


def _require_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HookError(f"{label} must be a table")
    return value


def _validate_unknown_keys(table: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise HookError(f"Unknown field(s) in {label}: {', '.join(sorted(unknown))}")


def _validate_bool(table: dict[str, Any], key: str, label: str) -> None:
    if key in table and type(table[key]) is not bool:
        raise HookError(f"{label}.{key} must be a TOML boolean")


def _validate_string(
    table: dict[str, Any], key: str, label: str, *, allow_none: bool = False
) -> None:
    if key in table and (table[key] is None and allow_none):
        return
    if key in table and not isinstance(table[key], str):
        raise HookError(f"{label}.{key} must be a string")


def _validate_string_list(table: dict[str, Any], key: str, label: str) -> None:
    if key not in table:
        return
    value = table[key]
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise HookError(f"{label}.{key} must be an array of strings")


def _validate_non_empty_string(table: dict[str, Any], key: str, label: str) -> None:
    _validate_string(table, key, label)
    if key in table and not table[key].strip():
        raise HookError(f"{label}.{key} must be a non-empty string")


def _validate_runner_command_placeholders(command: list[str] | tuple[str, ...], label: str, transport: str, model: Any) -> None:
    prompt_count = 0
    for index, argument in enumerate(command, start=1):
        argument_label = f"{label}.command[{index}]"
        for placeholder in RUNNER_PLACEHOLDER_PATTERN.findall(argument):
            if placeholder not in RUNNER_PLACEHOLDERS:
                raise HookError(f"Unknown placeholder {placeholder!r} in {argument_label}")
        if "{" in argument or "}" in argument:
            if argument not in RUNNER_PLACEHOLDERS:
                raise HookError(
                    f"Placeholders in {argument_label} must be whole argv elements"
                )
        if argument == "{prompt}":
            prompt_count += 1
    if transport == "stdin" and prompt_count:
        raise HookError(f"{label}.command must not contain {{prompt}} with stdin transport")
    if transport == "argv" and prompt_count != 1:
        raise HookError(
            f"{label}.command must contain exactly one {{prompt}} with argv transport"
        )
    if "{model}" in command and not model:
        raise HookError(f"{label}.command uses {{model}} but {label}.model is not configured")


def _validate_runner_profiles(raw: dict[str, Any]) -> None:
    runners = _require_table(raw.get("runners", {}), "runners")
    for name, profile_value in runners.items():
        if not isinstance(name, str) or not name.strip():
            raise HookError("runners profile names must be non-empty strings")
        label = f"runners.{name}"
        profile = _require_table(profile_value, label)
        _validate_unknown_keys(profile, RUNNER_KEYS, label)
        if "type" not in profile:
            raise HookError(f"{label}.type is required")
        _validate_non_empty_string(profile, "type", label)
        runner_type = profile["type"].strip()
        if runner_type not in RUNNER_TYPES:
            raise HookError(f"{label}.type must be one of: {', '.join(sorted(RUNNER_TYPES))}")
        _validate_string(profile, "model", label)
        if "model" in profile and not profile["model"].strip():
            raise HookError(f"{label}.model must be a non-empty string when provided")
        _validate_string(profile, "variant", label)
        _validate_string(profile, "project_access", label)
        _validate_string(profile, "prompt_transport", label)
        if "project_access" in profile and profile["project_access"] not in PROJECT_ACCESS_VALUES:
            raise HookError(f"{label}.project_access must be one of: artifacts, project")

        type_specific_keys = {
            "variant": runner_type == "opencode",
            "command": runner_type == "command",
            "prompt_transport": runner_type == "command",
        }
        for key, applicable in type_specific_keys.items():
            if key in profile and not applicable:
                raise HookError(f"{label}.{key} is only valid for runner type {('opencode' if key == 'variant' else 'command')}")

        if runner_type != "command":
            continue
        if "command" not in profile:
            raise HookError(f"{label}.command is required for runner type command")
        _validate_string_list(profile, "command", label)
        command = profile["command"]
        if not command:
            raise HookError(f"{label}.command must be a non-empty array")
        for index, argument in enumerate(command, start=1):
            if not argument.strip():
                raise HookError(f"{label}.command[{index}] must be a non-empty string")
        transport = profile.get("prompt_transport", "stdin")
        if transport not in PROMPT_TRANSPORT_VALUES:
            raise HookError(f"{label}.prompt_transport must be one of: argv, stdin")
        _validate_runner_command_placeholders(command, label, transport, profile.get("model"))


def _validate_integer(
    table: dict[str, Any], key: str, label: str, *, minimum: int | None = None
) -> None:
    if key not in table:
        return
    value = table[key]
    if type(value) is not int:
        raise HookError(f"{label}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise HookError(f"{label}.{key} must be at least {minimum}")


def _validate_config_types(raw: dict[str, Any]) -> None:
    unknown = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise HookError(
            "Legacy or unsupported config keys are not allowed: " + ", ".join(sorted(unknown))
        )

    general = _require_table(raw.get("general", {}), "general")
    _validate_unknown_keys(general, GENERAL_KEYS, "general")
    for key in (
        "enabled",
        "allow_push_on_error",
        "require_clean_worktree",
        "skip_on_sync_branch",
    ):
        _validate_bool(general, key, "general")
    _validate_string(general, "base_branch", "general")

    llm = _require_table(raw.get("llm", {}), "llm")
    _validate_unknown_keys(llm, LLM_KEYS, "llm")
    for key in ("runner", "model", "variant", "session_title_prefix"):
        _validate_string(llm, key, "llm")
    _validate_non_empty_string(llm, "runner", "llm")
    for key in ("json_retry_new_session", "delete_session_after_run"):
        _validate_bool(llm, key, "llm")
    _validate_integer(llm, "timeout_seconds", "llm", minimum=1)
    _validate_integer(llm, "max_parallel", "llm", minimum=1)
    _validate_integer(llm, "json_max_retries", "llm", minimum=0)
    _validate_integer(llm, "invalid_json_feedback_max_chars", "llm", minimum=1)
    _validate_integer(llm, "max_diff_bytes", "llm", minimum=1)

    logging = _require_table(raw.get("logging", {}), "logging")
    _validate_unknown_keys(logging, LOGGING_KEYS, "logging")
    for key in ("level", "dir", "transcript_dir", "summary_dir"):
        _validate_string(logging, key, "logging")
    for key in ("jsonl", "capture_llm_transcript", "print_llm_output"):
        _validate_bool(logging, key, "logging")

    workflow = _require_table(raw.get("workflow", {}), "workflow")
    _validate_unknown_keys(workflow, {"modules"}, "workflow")
    _validate_string_list(workflow, "modules", "workflow")

    modules = _require_table(raw.get("modules", {}), "modules")
    for module_id, module_value in modules.items():
        if not isinstance(module_id, str):
            raise HookError("modules keys must be strings")
        module = _require_table(module_value, f"modules.{module_id}")
        _validate_unknown_keys(module, {"enabled", "steps"}, f"modules.{module_id}")
        _validate_bool(module, "enabled", f"modules.{module_id}")
        if "steps" in module:
            steps = module["steps"]
            if not isinstance(steps, (list, tuple)):
                raise HookError(f"modules.{module_id}.steps must be an array of tables")
            for index, step_value in enumerate(steps, start=1):
                step = _require_table(step_value, f"modules.{module_id}.steps[{index}]")
                label = f"modules.{module_id}.steps[{index}]"
                _validate_unknown_keys(step, STEP_KEYS, label)
                for key in ("id", "type"):
                    _validate_string(step, key, label)
                for key in (
                    "collector",
                    "executor",
                    "assertion",
                    "output",
                    "schema",
                    "prompt",
                    "prompt_file",
                    "fallback_prompt_id",
                    "when_env",
                    "runner",
                ):
                    _validate_string(step, key, label, allow_none=True)
                for key in ("inputs", "allow_paths"):
                    _validate_string_list(step, key, label)
                if "runner" in step and step["runner"] is not None and not step["runner"].strip():
                    raise HookError(f"{label}.runner must be a non-empty string")
                if (
                    step.get("runner") is not None
                    and isinstance(step.get("type"), str)
                    and step["type"] in {"collect", "exec", "assert"}
                ):
                    raise HookError(f"{label}.runner is only valid on llm and apply steps")

    _validate_runner_profiles(raw)


def _normalize_runner_profile(name: str, raw: dict[str, Any]) -> RunnerProfile:
    runner_type = str(raw["type"]).strip()
    return RunnerProfile(
        name=name,
        type=runner_type,
        model=str(raw["model"]) if raw.get("model") is not None else None,
        variant=str(raw["variant"]) if raw.get("variant") is not None else None,
        project_access=str(
            raw.get(
                "project_access",
                "artifacts" if runner_type == "opencode" else "project",
            )
        ),
        command=tuple(str(item) for item in raw.get("command", []) or []),
        prompt_transport=str(raw.get("prompt_transport", "stdin")),
    )


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
        runner=str(raw.get("runner")).strip() if raw.get("runner") is not None else None,
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
    if not isinstance(raw, dict):
        raise HookError("Config document must contain a top-level table")
    _validate_config_types(raw)
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
    runner_payload = raw.get("runners", {})
    runners = {
        name: _normalize_runner_profile(name, profile)
        for name, profile in runner_payload.items()
    }
    if llm.runner != "opencode" and llm.runner not in runners:
        raise HookError(f"llm.runner references missing runner profile `{llm.runner}`")
    for module in modules.values():
        for index, step in enumerate(module.steps, start=1):
            if step.runner is None:
                continue
            if step.runner != "opencode" and step.runner not in runners:
                raise HookError(
                    f"modules.{module.id}.steps[{index}].runner references missing runner profile "
                    f"`{step.runner}`"
                )
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
        runners=runners,
    )


def resolve_runner_profile(
    config: HookConfig,
    step: StepConfig,
    env: Mapping[str, str] | None = None,
) -> RunnerProfile:
    """Resolve the runner selected by a promptable step and apply final env overrides."""
    if step.type not in {"llm", "apply"} and step.runner is not None:
        raise HookError(f"Step `{step.id}` may not select a runner")

    selected_name = step.runner or config.llm.runner
    profile = config.runners.get(selected_name)
    if profile is None:
        if selected_name != "opencode":
            raise HookError(f"Runner profile `{selected_name}` does not exist")
        profile = RunnerProfile(
            name="opencode",
            type="opencode",
            model=config.llm.model,
            variant=config.llm.variant,
            project_access="artifacts",
        )

    environment = os.environ if env is None else env
    model = profile.model
    model_override = environment.get("AI_PUSH_HOOKS_MODEL")
    if model_override is not None:
        if not model_override:
            raise HookError("AI_PUSH_HOOKS_MODEL must be a non-empty model identifier")
        model = model_override

    variant = profile.variant
    if profile.type == "opencode":
        variant_override = environment.get("AI_PUSH_HOOKS_VARIANT")
        if variant_override is not None:
            variant = variant_override.strip()

    return RunnerProfile(
        name=selected_name,
        type=profile.type,
        model=model,
        variant=variant,
        project_access=profile.project_access,
        command=profile.command,
        prompt_transport=profile.prompt_transport,
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
        "runners": {},
    }
    for module_id, module in config.modules.items():
        raw["modules"][module_id] = {
            "enabled": module.enabled,
            "steps": [step.__dict__.copy() for step in module.steps],
        }
    for name, profile in config.runners.items():
        runner_raw: dict[str, Any] = {
            "type": profile.type,
            "project_access": profile.project_access,
        }
        if profile.model is not None:
            runner_raw["model"] = profile.model
        if profile.variant is not None:
            runner_raw["variant"] = profile.variant
        if profile.type == "command":
            runner_raw["command"] = list(profile.command)
            runner_raw["prompt_transport"] = profile.prompt_transport
        raw["runners"][name] = runner_raw

    def read_env_bool(name: str) -> bool | None:
        value = os.getenv(name)
        if value is None:
            return None
        parsed = env_bool(name)
        if parsed is None:
            raise HookError(
                f"Invalid boolean environment override {name}: expected true/false"
            )
        return parsed

    skip = read_env_bool("AI_PUSH_HOOKS_SKIP")
    if skip is True:
        raw["general"]["enabled"] = False
    allow_on_error = read_env_bool("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR")
    if allow_on_error is not None:
        raw["general"]["allow_push_on_error"] = allow_on_error
    require_clean = read_env_bool("AI_PUSH_HOOKS_REQUIRE_CLEAN")
    if require_clean is not None:
        raw["general"]["require_clean_worktree"] = require_clean
    allow_dirty = read_env_bool("AI_PUSH_HOOKS_ALLOW_DIRTY")
    if allow_dirty is True:
        raw["general"]["require_clean_worktree"] = False
    base_branch = os.getenv("AI_PUSH_HOOKS_BASE_BRANCH")
    if base_branch:
        raw["general"]["base_branch"] = base_branch.strip() or "main"

    logging_level = os.getenv("AI_PUSH_HOOKS_LOG_LEVEL")
    if logging_level:
        raw["logging"]["level"] = logging_level.strip().lower()
    print_output = read_env_bool("AI_PUSH_HOOKS_PRINT_LLM_OUTPUT")
    if print_output is not None:
        raw["logging"]["print_llm_output"] = print_output
    model = os.getenv("AI_PUSH_HOOKS_MODEL")
    if model:
        raw["llm"]["model"] = model
    variant = os.getenv("AI_PUSH_HOOKS_VARIANT")
    if variant is not None:
        raw["llm"]["variant"] = variant.strip()
    timeout = os.getenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout.strip())
        except ValueError as exc:
            raise HookError(
                "Invalid numeric environment override AI_PUSH_HOOKS_TIMEOUT_SECONDS: "
                f"{timeout!r}"
            ) from exc
        if parsed_timeout < 1:
            raise HookError(
                "Invalid numeric environment override AI_PUSH_HOOKS_TIMEOUT_SECONDS: "
                "must be at least 1"
            )
        raw["llm"]["timeout_seconds"] = parsed_timeout
    return _build_config(raw)


def load_config(repo_root: pathlib.Path) -> tuple[HookConfig, pathlib.Path]:
    config_path = repo_root / "ai-push-hooks.toml"
    if not config_path.exists():
        raise HookError(
            "Missing required config file `ai-push-hooks.toml` in repo root. "
            "Run `ai-push-hooks init --template minimal-docs` first"
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HookError(f"Config file is not valid UTF-8: {config_path}") from exc
    except OSError as exc:
        raise HookError(f"Could not read config file {config_path}: {exc}") from exc
    try:
        loaded = tomllib.loads(text)
    except ValueError as exc:
        location = ""
        if hasattr(exc, "lineno") and hasattr(exc, "colno"):
            location = f" at line {exc.lineno}, column {exc.colno}"
        raise HookError(f"Invalid TOML in {config_path}{location}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise HookError(f"Invalid config format in {config_path}: expected a top-level table")
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
