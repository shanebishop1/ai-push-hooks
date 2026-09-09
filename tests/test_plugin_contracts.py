from __future__ import annotations

import pathlib
from dataclasses import fields
from types import MappingProxyType

import pytest

from ai_push_hooks.plugins import (
    CollectorResult,
    PluginContext,
    PushContext,
    validate_assert_result,
    validate_collector_result,
    validate_exec_result,
)
from ai_push_hooks.types import HookError, HookLogger, PushRefUpdate


def _push_context() -> PushContext:
    update = PushRefUpdate(
        local_ref="refs/heads/feature/test",
        local_sha="a" * 40,
        remote_ref="refs/heads/feature/test",
        remote_sha="b" * 40,
    )
    return PushContext(
        branch_name="feature/test",
        checked_out_branch="feature/test",
        base_branch="main",
        ranges=["main..HEAD"],
        changed_files=["README.md"],
        diff_text="diff --git a/README.md b/README.md\n",
        push_updates=[update],
    )


def test_public_context_records_are_frozen_and_flat() -> None:
    assert {field.name for field in fields(PushContext)} == {
        "branch_name",
        "checked_out_branch",
        "base_branch",
        "ranges",
        "changed_files",
        "diff_text",
        "push_updates",
    }
    assert {field.name for field in fields(PluginContext)} == {
        "repo_root",
        "module_id",
        "step_id",
        "inputs",
        "options",
        "prior_module_metadata",
        "push",
        "logger",
    }

    context = PluginContext(
        repo_root=pathlib.Path("/repo"),
        module_id="quality",
        step_id="policy",
        inputs={"collect/result.json": pathlib.Path("/repo/result.json")},
        options={"nested": {"items": ["one"]}},
        prior_module_metadata={"seen": {"count": 1}},
        push=_push_context(),
        logger=HookLogger(jsonl_path=None),
    )

    assert isinstance(context.inputs, MappingProxyType)
    assert isinstance(context.options, MappingProxyType)
    assert isinstance(context.options["nested"], MappingProxyType)
    assert context.options["nested"]["items"] == ("one",)
    assert context.push.ranges == ("main..HEAD",)
    assert context.push.push_updates[0].operation == "update"

    with pytest.raises(TypeError):
        context.inputs["new"] = pathlib.Path("/repo/new")  # type: ignore[index]
    with pytest.raises(TypeError):
        context.options["nested"]["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        context.prior_module_metadata["seen"]["count"] = 2  # type: ignore[index]


def test_context_snapshots_are_not_live_mappings() -> None:
    options = {"items": [1]}
    metadata = {"nested": {"value": "before"}}
    context = PluginContext(
        repo_root=pathlib.Path("/repo"),
        module_id="m",
        step_id="s",
        inputs={},
        options=options,
        prior_module_metadata=metadata,
        push=_push_context(),
        logger=HookLogger(jsonl_path=None),
    )

    options["items"].append(2)
    metadata["nested"]["value"] = "after"

    assert context.options["items"] == (1,)
    assert context.prior_module_metadata["nested"]["value"] == "before"


@pytest.mark.parametrize(
    "value",
    [None, {"ok": "false"}, {"ok": 1}, {"ok": True, "message": 2}],
)
def test_assert_result_requires_strict_contract(value: object) -> None:
    with pytest.raises(HookError):
        validate_assert_result(value)


def test_result_validators_accept_contracts_and_reject_non_json_values() -> None:
    collector = CollectorResult(
        artifacts={"result.json": {"items": [1, "two"]}},
        metadata={"count": 2},
    )
    assert validate_collector_result(collector) is collector
    assert validate_exec_result({"returncode": 0, "stdout": ""}) == {
        "returncode": 0,
        "stdout": "",
    }
    assert validate_assert_result({"ok": False, "message": "blocked"})["ok"] is False

    with pytest.raises(HookError, match="JSON-serializable"):
        validate_exec_result({"bad": object()})
    with pytest.raises(HookError, match="artifact name"):
        validate_collector_result(CollectorResult(artifacts={"../bad": "x"}))
