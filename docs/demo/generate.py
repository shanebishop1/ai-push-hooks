"""Generate the deterministic review/fix demo GIF.

Run with ``uv run --no-project --with pillow==11.3.0 python
docs/demo/generate.py``. Pillow is a demo-only dependency; no runtime or
development dependency is added.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


ROOT = pathlib.Path(__file__).resolve().parents[2]
OUTPUT = pathlib.Path(__file__).with_name("review-fix.gif")
SIZE = (960, 540)
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
)
RUNNER_SOURCE = textwrap.dedent(
    """\
    import json
    import pathlib
    import sys

    readme = pathlib.Path.cwd() / "README.md"
    content = readme.read_text(encoding="utf-8")
    if "TODO:" not in content:
        print("[]")
    elif "apply" in sys.stdin.read().lower():
        readme.write_text(
            content.replace("TODO: replace this note.", "Ready: this note is reviewed."),
            encoding="utf-8",
        )
        print("applied deterministic fix")
    else:
        print(json.dumps([{"file": "README.md", "description": "TODO remains"}]))
    """
)
CALLBACK_SOURCE = textwrap.dedent(
    """\
    import json


    def assert_review(context):
        issues = json.loads(
            context.inputs["inspect/issues.json"].read_text(encoding="utf-8")
        )
        return {
            "ok": not issues,
            "message": "Review found documentation issues." if issues else "",
        }
    """
)
BASE_CONFIG = """[general]
allow_push_on_error = false
skip_on_sync_branch = false

[llm]
runner = "deterministic"

[runners.deterministic]
type = "command"
command = [{command}]
project_access = "project"

[logging]
jsonl = false
capture_llm_transcript = false

[workflow]
modules = ["review"]

[modules.review]
enabled = true

[[modules.review.steps]]
id = "inspect"
type = "ask"
runner = "deterministic"
prompt = "Review README.md. Return JSON issues only."
output = "issues.json"
"""
APPLY_STEPS = """[[modules.review.steps]]
id = "apply"
type = "apply"
runner = "deterministic"
prompt = "Apply the reviewed fix to README.md."
inputs = ["inspect/issues.json"]
allow_paths = ["README.md"]

[[modules.review.steps]]
id = "manual-commit"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
"""
GATE_STEP = """[[modules.review.steps]]
id = "gate"
type = "assert"
python = "demo_hooks.py:assert_review"
inputs = ["inspect/issues.json"]
"""
SCENES = (
    "$ AI-PUSH-HOOKS HOOK DEMO|normal\nREVIEW: 1 FINDING IN README.MD|warn\nACTUAL EXIT: {0}|bad",
    "REVIEW GATE|normal\nFAIL: REVIEW FOUND DOCUMENTATION ISSUES|bad\nPUSH BLOCKED / ACTUAL EXIT: {0}|bad",
    "$ AI-PUSH-HOOKS HOOK DEMO|normal\nFIX APPLIED / PUSH BLOCKED|warn\nMANUAL GATE / ACTUAL EXIT: {1}|bad\nREVIEW AND COMMIT BEFORE RETRY|bad",
    '$ GIT DIFF README.MD|normal\n$ GIT ADD README.MD|normal\n$ git commit -m "fix demo documentation"|good\nLOCAL COMMIT CREATED|good',
    "$ AI-PUSH-HOOKS HOOK DEMO|normal\nREVIEW: 0 FINDINGS|good\nAPPLY: NO CHANGES|muted\nPASS / ACTUAL EXIT: {2}|good",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


def run(
    args: list[str],
    cwd: pathlib.Path,
    env: dict[str, str],
    *,
    input_text: str = "",
) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return CommandResult(
        completed.returncode, (completed.stdout + completed.stderr).strip()
    )


def git(repo: pathlib.Path, env: dict[str, str], *args: str) -> str:
    result = run(["git", *args], repo, env)
    if result.returncode:
        raise RuntimeError(result.output or f"git {' '.join(args)} failed")
    return result.output


def isolated_environment(repo: pathlib.Path) -> dict[str, str]:
    credential_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AI_PUSH_HOOKS_", "GIT_"))
        and not any(marker in key.upper() for marker in credential_markers)
    }
    home = repo / ".home"
    xdg = repo / ".xdg"
    home.mkdir()
    xdg.mkdir()
    environment.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    return environment


def write_fixture(repo: pathlib.Path, runner: pathlib.Path) -> None:
    (repo / "README.md").write_text(
        "# Demo\n\nTODO: replace this note.\n", encoding="utf-8"
    )
    runner.write_text(RUNNER_SOURCE, encoding="utf-8")
    (repo / "demo_hooks.py").write_text(CALLBACK_SOURCE, encoding="utf-8")


def write_config(repo: pathlib.Path, runner: pathlib.Path, *, apply: bool) -> None:
    command = ", ".join(json.dumps(value) for value in (sys.executable, str(runner)))
    steps = APPLY_STEPS if apply else GATE_STEP
    (repo / "ai-push-hooks.toml").write_text(
        BASE_CONFIG.format(command=command) + steps, encoding="utf-8"
    )


def invoke(
    repo: pathlib.Path,
    env: dict[str, str],
    local_sha: str,
    remote_sha: str,
) -> CommandResult:
    push_line = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    return run(
        [sys.executable, "-m", "ai_push_hooks", "hook", "demo", "fixture"],
        repo,
        env,
        input_text=push_line,
    )


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(lines: list[tuple[str, str]]) -> Image.Image:
    image = Image.new("RGB", SIZE, "#0e161e")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, SIZE[0], 18), fill="#1f303b")
    draw.rounded_rectangle((36, 66, 924, 504), radius=4, fill="#141f29")
    draw.rectangle((36, 66, 44, 504), fill="#35beb5")
    draw.text(
        (60, 24), "AI PUSH HOOKS / DETERMINISTIC DEMO", font=font(24), fill="#88e0d6"
    )
    draw.text(
        (60, 76),
        "MOCK RUNNER / NO LIVE AI / SAFE TEMP REPOSITORY",
        font=font(16),
        fill="#758894",
    )
    for index, (line, color) in enumerate(lines):
        draw.text((66, 124 + index * 42), line, font=font(24), fill=color)
    return image


def rendered_scenes(results: list[CommandResult]) -> list[list[str]]:
    statuses = tuple(str(result.returncode) for result in results)
    scenes = [scene.format(*statuses).splitlines() for scene in SCENES]
    expected = (
        "ACTUAL EXIT: 1",
        "PUSH BLOCKED / ACTUAL EXIT: 1",
        "MANUAL GATE / ACTUAL EXIT: 1",
        "LOCAL COMMIT CREATED",
        "PASS / ACTUAL EXIT: 0",
    )
    if statuses != ("1", "1", "0"):
        raise RuntimeError(f"unexpected demo hook exits: {statuses}")
    for scene, marker in zip(scenes, expected):
        if not any(marker in line for line in scene):
            raise RuntimeError(f"demo frame is missing expected result: {marker}")
    return scenes


def make_frames(results: list[CommandResult]) -> list[Image.Image]:
    colors = {
        "normal": "#e8f0f4",
        "muted": "#758894",
        "good": "#75e09a",
        "warn": "#f6cd6b",
        "bad": "#ff7c70",
    }
    frames = rendered_scenes(results)
    return [
        render(
            [
                (line, colors[color])
                for line, color in (item.rsplit("|", 1) for item in frame)
            ]
        )
        for frame in frames
    ]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-push-hooks-demo-") as temporary:
        repo = pathlib.Path(temporary) / "fixture"
        repo.mkdir()
        env = isolated_environment(repo)
        git(repo, env, "init", "-b", "main")
        git(repo, env, "config", "user.name", "Demo Runner")
        git(repo, env, "config", "user.email", "demo@example.invalid")
        runner = repo / "deterministic_runner.py"
        write_fixture(repo, runner)
        git(repo, env, "add", "README.md", "demo_hooks.py", "deterministic_runner.py")
        git(repo, env, "commit", "-m", "seed deterministic demo")
        initial_sha = git(repo, env, "rev-parse", "HEAD")

        write_config(repo, runner, apply=False)
        review = invoke(repo, env, initial_sha, "0" * 40)
        if (
            review.returncode == 0
            or "Review found documentation issues." not in review.output
        ):
            raise RuntimeError("demo review did not fail with the expected assertion")

        write_config(repo, runner, apply=True)
        applied = invoke(repo, env, initial_sha, "0" * 40)
        readme = (repo / "README.md").read_text(encoding="utf-8")
        if (
            applied.returncode == 0
            or "Documentation updates were applied; review and commit them before pushing again."
            not in applied.output
            or "Ready: this note is reviewed." not in readme
        ):
            raise RuntimeError("demo apply did not change README and block as expected")

        diff = git(repo, env, "diff", "--", "README.md")
        if (
            "-TODO: replace this note." not in diff
            or "+Ready: this note is reviewed." not in diff
        ):
            raise RuntimeError("demo diff did not contain the applied README change")
        git(repo, env, "add", "README.md")
        git(repo, env, "commit", "-m", "fix demo documentation")
        fixed_sha = git(repo, env, "rev-parse", "HEAD")
        passed = invoke(repo, env, fixed_sha, initial_sha)
        if passed.returncode != 0:
            raise RuntimeError(passed.output or "demo pass unexpectedly failed")

        frames = make_frames([review, applied, passed])
        frames[0].save(
            OUTPUT,
            save_all=True,
            append_images=frames[1:],
            duration=3000,
            loop=0,
            optimize=True,
        )
    print(f"generated {OUTPUT} ({OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
