from __future__ import annotations

import pathlib

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.llm import OpenCodeRunResult, run_llm_step

from .conftest import build_context, init_repo


def test_run_llm_step_accepts_array_for_docs_issue_schema(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    analyze_step = next(step for step in config.modules["docs"].steps if step.id == "analyze")

    def fake_call_opencode(*args, **kwargs):
        return OpenCodeRunResult(
            output_text="[]",
            session_id=None,
            stdout="",
            stderr="",
            return_code=0,
        )

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call_opencode)
    monkeypatch.setattr("ai_push_hooks.executors.llm.finalize_opencode_session", lambda *args, **kwargs: None)

    payload = run_llm_step(context, analyze_step, "prompt", [], "docs.analyze")

    assert payload == []
