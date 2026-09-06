from __future__ import annotations

import json

from .opencode_contract_smoke import MockProvider, workspace_path


def test_workspace_path_preserves_case_for_permission_matching() -> None:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": "Working directory: /tmp/OpenCode-Contract/Synthetic-Repo",
            }
        ]
    }

    assert workspace_path(payload) == "/tmp/OpenCode-Contract/Synthetic-Repo"


def test_mock_provider_write_uses_case_preserved_workspace_path() -> None:
    provider = MockProvider()
    payload = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "[SMOKE_APPLY]\n"
                    "Working directory: /tmp/OpenCode-Contract/Synthetic-Repo"
                ),
            },
            {"role": "tool", "content": "synthetic read result"},
        ]
    }

    kind, calls = provider.response_plan(payload)

    assert kind == "tools"
    assert calls[0]["function"]["name"] == "write"
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert arguments["filePath"] == "/tmp/OpenCode-Contract/Synthetic-Repo/README.md"


def test_mock_provider_requests_all_readonly_forbidden_tool_classes() -> None:
    provider = MockProvider()

    kind, calls = provider.response_plan(
        {"messages": [{"role": "user", "content": "[SMOKE_READONLY]"}]}
    )

    assert kind == "tools"
    assert {call["function"]["name"] for call in calls} == {
        "read",
        "bash",
        "task",
        "webfetch",
        "websearch",
    }
