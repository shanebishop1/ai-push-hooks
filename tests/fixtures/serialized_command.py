from __future__ import annotations

import os
import pathlib
import sys
import time


lock = pathlib.Path(os.environ["AI_PUSH_HOOKS_SERIAL_COMMAND_LOCK"])
events = pathlib.Path(os.environ["AI_PUSH_HOOKS_SERIAL_COMMAND_EVENTS"])
stage = sys.argv[1]

if lock.exists():
    events.open("a", encoding="utf-8").write(f"overlap:{stage}\n")
    raise SystemExit(9)

lock.touch()
try:
    with events.open("a", encoding="utf-8") as handle:
        handle.write(f"start:{stage}\n")
    if stage.endswith("-assert"):
        sys.stdin.read()
    time.sleep(0.05)
    with events.open("a", encoding="utf-8") as handle:
        handle.write(f"end:{stage}\n")
finally:
    lock.unlink(missing_ok=True)
