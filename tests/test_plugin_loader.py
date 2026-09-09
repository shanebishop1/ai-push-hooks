from __future__ import annotations

import os
import pathlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_push_hooks import plugin_loader as plugin_loader_module
from ai_push_hooks.plugin_loader import PluginDispatcher, PluginLoader
from ai_push_hooks.plugins import PluginContext
from ai_push_hooks.types import (
    GeneralConfig,
    HookConfig,
    HookError,
    HookLogger,
    LlmConfig,
    LoggingConfig,
    ModuleConfig,
    ModuleRuntimeState,
    PushRefUpdate,
    RuntimeContext,
    StepConfig,
    WorkflowConfig,
)


def _runtime(
    repo: pathlib.Path,
    module: ModuleConfig,
    *,
    metadata: dict[str, object] | None = None,
    cache: dict[str, object] | None = None,
) -> tuple[RuntimeContext, ModuleRuntimeState]:
    config = HookConfig(
        general=GeneralConfig(base_branch="main"),
        llm=LlmConfig(),
        logging=LoggingConfig(jsonl=False),
        workflow=WorkflowConfig(modules=(module.id,)),
        modules={module.id: module},
    )
    context = RuntimeContext(
        repo_root=repo,
        git_dir=repo / ".git",
        config=config,
        logger=HookLogger(jsonl_path=None),
        remote_name="origin",
        remote_url="https://example.invalid/repo.git",
        stdin_lines=[],
        run_id="run",
        run_dir=repo / ".run",
        cache=cache or {},
    )
    return context, ModuleRuntimeState(module=module, metadata=metadata or {})


def _step(reference: str, *, kind: str = "collect", inputs: tuple[str, ...] = ()) -> StepConfig:
    return StepConfig(id="hook", type=kind, python=reference, inputs=inputs)


def _write(repo: pathlib.Path, name: str, source: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_loader_is_lazy_and_imports_once(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "imports.txt"
    _write(
        tmp_path,
        "checks.py",
        f"open({str(marker)!r}, 'a', encoding='utf-8').write('import\\n')\n"
        "def hook(context):\n    return 7\n",
    )
    loader = PluginLoader()
    assert not marker.exists()

    callback = loader.load(tmp_path, "checks.py:hook")
    assert marker.read_text(encoding="utf-8") == "import\n"
    assert callback(object()) == 7
    loader.invoke(tmp_path, "checks.py:hook", object())
    assert marker.read_text(encoding="utf-8") == "import\n"


def test_source_replacement_does_not_hot_reload(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "checks.py", "def hook(context):\n    return 'old'\n")
    loader = PluginLoader()
    assert loader.invoke(tmp_path, "checks.py:hook", object()) == "old"
    (tmp_path / "checks.py").write_text(
        "def hook(context):\n    return 'new'\n", encoding="utf-8"
    )
    assert loader.invoke(tmp_path, "checks.py:hook", object()) == "old"


def test_same_filename_in_two_repositories_has_separate_cache(tmp_path: pathlib.Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write(first, "checks.py", "def hook(context):\n    return 'first'\n")
    _write(second, "checks.py", "def hook(context):\n    return 'second'\n")
    loader = PluginLoader()

    assert loader.invoke(first, "checks.py:hook", object()) == "first"
    assert loader.invoke(second, "checks.py:hook", object()) == "second"


def test_concurrent_load_executes_source_once(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "imports.txt"
    _write(
        tmp_path,
        "checks.py",
        f"open({str(marker)!r}, 'a', encoding='utf-8').write('x\\n')\n"
        "def hook(context):\n    return 1\n",
    )
    loader = PluginLoader()
    barrier = threading.Barrier(8)

    def invoke() -> int:
        barrier.wait()
        return loader.invoke(tmp_path, "checks.py:hook", object())

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: invoke(), range(8))) == [1] * 8
    assert marker.read_text(encoding="utf-8") == "x\n"


def test_loader_rejects_symlink_and_traversal(tmp_path: pathlib.Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def hook(context): return True\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link.py").symlink_to(outside)
    loader = PluginLoader()

    with pytest.raises(HookError, match="symlink|reparse"):
        loader.load(repo, "link.py:hook")
    with pytest.raises(HookError, match="must not contain '..'"):
        loader.load(repo, "../outside.py:hook")


def test_descriptor_relative_load_rejects_parent_symlink_replacement(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not plugin_loader_module._descriptor_relative_supported():
        pytest.skip("descriptor-relative no-follow traversal is unavailable")

    repo = tmp_path / "repo"
    parent = repo / "checks"
    outside = tmp_path / "outside"
    repo.mkdir()
    parent.mkdir()
    outside.mkdir()
    _write(parent, "hook.py", "def hook(context): return 'inside'\n")
    _write(outside, "hook.py", "def hook(context): return 'outside'\n")

    original_open = plugin_loader_module._OS_OPEN
    swapped = False

    def replace_parent_after_root_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == repo and "dir_fd" not in kwargs and not swapped:
            swapped = True
            parent.rename(repo / "real-checks")
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(plugin_loader_module, "_OS_OPEN", replace_parent_after_root_open)
    with pytest.raises(HookError, match="symlink|reparse"):
        PluginLoader().load(repo, "checks/hook.py:hook")
    assert swapped


def test_mapping_inputs_reject_undeclared_keys_before_import(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "imported"
    _write(
        tmp_path,
        "checks.py",
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('bad')\n"
        "def hook(context): return context\n",
    )
    module = ModuleConfig(id="quality", enabled=True, steps=())
    runtime, state = _runtime(tmp_path, module)
    step = StepConfig(id="hook", type="collect", python="checks.py:hook", inputs=("declared",))
    declared = tmp_path / "declared"
    extra = tmp_path / "extra"
    declared.write_text("declared", encoding="utf-8")
    extra.write_text("extra", encoding="utf-8")

    with pytest.raises(HookError, match="Undeclared Python plugin input"):
        PluginDispatcher().dispatch(
            runtime,
            state,
            step,
            {"extra": extra, "declared": declared},
        )
    assert not marker.exists()


def test_mapping_inputs_follow_declared_order(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "checks.py", "def hook(context): return tuple(context.inputs)\n")
    module = ModuleConfig(id="quality", enabled=True, steps=())
    runtime, state = _runtime(tmp_path, module)
    step = StepConfig(
        id="hook",
        type="collect",
        python="checks.py:hook",
        inputs=("first", "second"),
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    assert PluginDispatcher().dispatch(
        runtime,
        state,
        step,
        {"second": second, "first": first},
    ) == ("first", "second")


def test_installed_dependency_uses_interpreter_environment_without_path_changes(
    tmp_path: pathlib.Path,
) -> None:
    _write(
        tmp_path,
        "checks.py",
        "import json\n"
        "def hook(context):\n    return json.dumps({'ok': True})\n",
    )
    loader = PluginLoader()
    original_path = list(sys.path)
    original_cwd = pathlib.Path.cwd()
    original_env = os.environ.copy()

    assert loader.invoke(tmp_path, "checks.py:hook", object()) == '{"ok": true}'
    assert sys.path == original_path
    assert pathlib.Path.cwd() == original_cwd
    assert os.environ.copy() == original_env


def test_context_contains_snapshots_and_prior_metadata(tmp_path: pathlib.Path) -> None:
    source = """
def hook(context):
    assert context.module_id == "quality"
    assert context.step_id == "hook"
    assert context.inputs["prior/result.json"].name == "result.json"
    assert context.options["nested"]["items"] == ("before",)
    assert context.prior_module_metadata["nested"]["value"] == "before"
    assert context.push.branch_name == "feature/test"
    assert context.push.base_branch == "main"
    assert context.push.ranges == ("main..HEAD",)
    return context
"""
    _write(tmp_path, "checks.py", source)
    update = PushRefUpdate("refs/heads/feature/test", "a" * 40, "refs/heads/feature/test", "b" * 40)
    module = ModuleConfig(id="quality", enabled=True, steps=())
    runtime, state = _runtime(
        tmp_path,
        module,
        metadata={"nested": {"value": "before"}},
        cache={
            "branch_name": "feature/test",
            "checked_out_branch": "feature/test",
            "ranges": ["main..HEAD"],
            "changed_files": ["README.md"],
            "diff_text": "diff",
            "push_updates": [update],
        },
    )
    step = StepConfig(
        id="hook",
        type="collect",
        python="checks.py:hook",
        inputs=("prior/result.json",),
        options={"nested": {"items": ["before"]}},
    )
    context_path = tmp_path / "result.json"
    context_path.write_text("{}", encoding="utf-8")
    result = PluginDispatcher().dispatch(runtime, state, step, [context_path])

    assert isinstance(result, PluginContext)
    runtime.cache["ranges"] = ["changed"]
    state.metadata["nested"]["value"] = "after"
    step.options["nested"]["items"].append("after")
    assert result.push.ranges == ("main..HEAD",)
    assert result.prior_module_metadata["nested"]["value"] == "before"
    assert result.options["nested"]["items"] == ("before",)
    with pytest.raises(TypeError):
        result.inputs["new"] = context_path  # type: ignore[index]


def test_plugin_prints_are_not_host_sanitized(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "checks.py", "def hook(context):\n    print('plugin output')\n    return None\n")
    PluginLoader().invoke(tmp_path, "checks.py:hook", object(), stage="collect")
    assert "plugin output" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import dependency_that_is_not_installed_anywhere\ndef hook(c): return 1\n", "dependency_that_is_not_installed_anywhere"),
        ("def hook(c): raise ValueError('secret payload')\n", "raised an exception"),
        ("raise SystemExit('secret payload')\ndef hook(c): return 1\n", "exited during import"),
    ],
)
def test_import_and_callback_failures_are_named_without_payloads(
    tmp_path: pathlib.Path, source: str, message: str
) -> None:
    _write(tmp_path, "checks.py", source)
    with pytest.raises(HookError) as raised:
        PluginLoader().invoke(tmp_path, "checks.py:hook", object(), stage="assert")
    assert message in str(raised.value)
    assert "secret payload" not in str(raised.value)


def test_missing_callable_is_named(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "checks.py", "value = 1\n")
    with pytest.raises(HookError, match=r"assert plugin checks\.py:missing"):
        PluginLoader().invoke(tmp_path, "checks.py:missing", object(), stage="assert")


def test_async_callbacks_and_awaitable_returns_are_rejected(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "async_hook.py", "async def hook(context): return 1\n")
    with pytest.raises(HookError, match="must be synchronous"):
        PluginLoader().invoke(tmp_path, "async_hook.py:hook", object())

    _write(
        tmp_path,
        "awaitable.py",
        "class Awaitable:\n"
        "    def __await__(self):\n"
        "        yield\n"
        "def hook(context): return Awaitable()\n",
    )
    with pytest.raises(HookError, match="must return synchronously"):
        PluginLoader().invoke(tmp_path, "awaitable.py:hook", object())


def test_system_exit_is_converted_and_keyboard_interrupt_propagates(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "exit.py", "def hook(context): raise SystemExit('secret')\n")
    with pytest.raises(HookError, match="assert plugin exit.py:hook exited") as raised:
        PluginLoader().invoke(tmp_path, "exit.py:hook", object(), stage="assert")
    assert "secret" not in str(raised.value)

    _write(tmp_path, "interrupt.py", "def hook(context): raise KeyboardInterrupt\n")
    with pytest.raises(KeyboardInterrupt):
        PluginLoader().invoke(tmp_path, "interrupt.py:hook", object())
