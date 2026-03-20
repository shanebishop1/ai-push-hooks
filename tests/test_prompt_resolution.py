from __future__ import annotations

import pathlib

import pytest

from ai_push_hooks.config import resolve_prompt_text
from ai_push_hooks.prompts_builtin import BUILTIN_PROMPTS
from ai_push_hooks.types import HookError, StepConfig


def test_inline_prompt_wins_over_file_and_builtin(tmp_path: pathlib.Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("file prompt", encoding="utf-8")
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt="inline prompt",
        prompt_file="prompt.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == "inline prompt"


def test_file_prompt_wins_over_builtin(tmp_path: pathlib.Path) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("file prompt", encoding="utf-8")
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt_file="prompt.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == "file prompt"


def test_missing_file_falls_back_to_builtin(tmp_path: pathlib.Path) -> None:
    step = StepConfig(
        id="query",
        type="llm",
        output="queries.json",
        schema="string_array",
        prompt_file="missing.txt",
        fallback_prompt_id="docs-query-basic",
    )
    assert resolve_prompt_text(tmp_path, step) == BUILTIN_PROMPTS["docs-query-basic"]


def test_missing_all_prompt_sources_fails(tmp_path: pathlib.Path) -> None:
    step = StepConfig(id="query", type="llm", output="queries.json", schema="string_array")
    with pytest.raises(HookError, match="No prompt source available"):
        resolve_prompt_text(tmp_path, step)
