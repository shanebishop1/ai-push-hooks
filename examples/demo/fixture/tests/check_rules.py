#!/usr/bin/env python3
"""Mechanical project checks.

Deliberately narrow. This file greps for the one AGENTS.md rule that has an
exact textual signature (no direct `fetch()` in components). The other rules --
whether a migration is safe under a rolling deploy, whether a docs paragraph is
filler -- have no regex, which is the reason the AI review step exists.

Used in the demo as the deterministic postcondition after `apply`: a runner
reporting success is not proof that the fix landed. This script is.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DIRECT_FETCH = re.compile(r"(?<![.\w])fetch\s*\(")


def main() -> int:
    failures = []
    components = REPO / "src" / "components"
    for path in sorted(components.rglob("*.tsx")):
        if DIRECT_FETCH.search(path.read_text(encoding="utf-8")):
            failures.append(
                f"{path.relative_to(REPO)}: direct fetch() call; "
                f"route it through src/api/client.ts (AGENTS.md rule 1)"
            )

    if failures:
        print("check_rules: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("check_rules: PASS (no direct fetch() in src/components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
