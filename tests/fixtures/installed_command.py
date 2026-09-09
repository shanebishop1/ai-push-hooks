from __future__ import annotations

import json
import sys


payload = json.loads(sys.stdin.read())
if payload.get("dependency") != "installed-interpreter":
    raise SystemExit(7)
print(json.dumps({"callback": "command", "module": payload["module"]}))
