#!/usr/bin/env python3
"""Run non-billable contract checks against installed runner CLIs.

This intentionally does not call an auth, status, login, or model command.  It
only asks installed binaries for their version/help text and reports whether
the pinned invocation contract is advertised.  Missing optional binaries are
skipped successfully.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


EXPECTED_VERSIONS = {
    "opencode": "1.18.29",
    "codex": "0.148.0",
    "claude": "2.1.220",
}

CODEX_REQUIRED_HELP = (
    "--json",
    "--color",
    "never",
    "--sandbox",
    "--ephemeral",
    "--cd",
    "--skip-git-repo-check",
    "--model",
    "-",
)

# Keep this list identical to the production Claude adapter.  Checking the
# permission spelling is important: accepting a nearby/legacy spelling would
# silently weaken the intended policy.
CLAUDE_REQUIRED_HELP = (
    "--print",
    "--output-format",
    "json",
    "--no-session-persistence",
    "--model",
    "--permission-mode",
    "dontAsk",
    "acceptEdits",
    "--tools",
    "--allowedTools",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        shell=False,
    )


def _safe_first_line(value: str) -> str:
    """Return version evidence without printing arbitrary CLI diagnostics."""

    for line in value.splitlines():
        line = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", line).strip()
        if line:
            return line[:240]
    return "<no version output>"


def _has_version(text: str, expected: str) -> bool:
    return re.search(rf"(?<![0-9]){re.escape(expected)}(?![0-9])", text) is not None


def _check_cli(
    name: str,
    executable: str,
    *,
    required_help: tuple[str, ...] = (),
    check_help: bool = False,
    run: CommandRunner = _run,
) -> CheckResult:
    expected = EXPECTED_VERSIONS[name]
    try:
        version = run((executable, "--version"))
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            name, "FAIL", f"could not run --version ({type(exc).__name__})"
        )
    if version.returncode != 0:
        return CheckResult(name, "FAIL", "--version returned non-zero")
    version_output = f"{version.stdout}\n{version.stderr}"
    if not _has_version(version_output, expected):
        return CheckResult(
            name,
            "FAIL",
            f"expected version {expected}; observed {_safe_first_line(version_output)!r}",
        )

    if not required_help and not check_help:
        return CheckResult(
            name, "PASS", f"--version: {_safe_first_line(version_output)}"
        )

    help_argv = (
        (executable, "exec", "--help") if name == "codex" else (executable, "--help")
    )
    try:
        help_result = run(help_argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(name, "FAIL", f"could not run --help ({type(exc).__name__})")
    if help_result.returncode != 0:
        return CheckResult(name, "FAIL", "--help returned non-zero")
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    missing = tuple(marker for marker in required_help if marker not in help_text)
    if missing:
        return CheckResult(
            name, "FAIL", "missing required help markers: " + ", ".join(missing)
        )
    return CheckResult(
        name,
        "PASS",
        f"--version: {_safe_first_line(version_output)}; required --help contract present",
    )


def installed_checks(*, run: CommandRunner = _run) -> list[CheckResult]:
    """Check only binaries present on PATH; never invoke a missing runner."""

    checks: list[CheckResult] = []
    opencode = shutil.which("opencode") or shutil.which("opencode-cli")
    if opencode is None:
        checks.append(CheckResult("opencode", "SKIP", "binary not installed"))
    else:
        checks.append(_check_cli("opencode", opencode, check_help=True, run=run))

    codex = shutil.which("codex")
    if codex is None:
        checks.append(CheckResult("codex", "SKIP", "binary not installed"))
    else:
        checks.append(
            _check_cli("codex", codex, required_help=CODEX_REQUIRED_HELP, run=run)
        )

    claude = shutil.which("claude")
    if claude is None:
        checks.append(CheckResult("claude", "SKIP", "binary not installed"))
    else:
        checks.append(
            _check_cli("claude", claude, required_help=CLAUDE_REQUIRED_HELP, run=run)
        )
    return checks


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    results = installed_checks()
    for result in results:
        print(f"{result.status}: {result.name}: {result.detail}")
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
