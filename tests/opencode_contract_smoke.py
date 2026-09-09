from __future__ import annotations

import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


EXPECTED_OPENCODE_VERSION = "1.18.29"
README_BEFORE = "Synthetic beta smoke repository.\n"
README_AFTER = README_BEFORE + "\nBeta smoke edit applied.\n"


def run(command: list[str], cwd: pathlib.Path | None = None, *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
        env=os.environ.copy(),
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def git(repo: pathlib.Path, *arguments: str) -> str:
    return run(["git", *arguments], repo).strip()


def write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.extend(flatten_strings(key))
            result.extend(flatten_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(flatten_strings(item))
        return result
    return []


def messages_from_request(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]
    messages = payload.get("input")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]
    return []


def tool_result_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for message in messages_from_request(payload):
        if message.get("role") in {"tool", "function"}:
            results.append(message)
            continue
        if message.get("type") == "function_call_output":
            results.append(message)
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and "tool-result" in str(part.get("type", ""))
            for part in content
        ):
            results.append(message)
    return results


def request_text(payload: dict[str, Any]) -> str:
    return "\n".join(flatten_strings(payload)).lower()


def workspace_path(payload: dict[str, Any]) -> str:
    # Keep the path's original case. Permission patterns and filenames are
    # case-sensitive on the Linux smoke target, even though prompt matching is
    # intentionally case-insensitive elsewhere in this mock.
    text = "\n".join(flatten_strings(payload))
    match = re.search(r"working directory:\s+([^\s]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).rstrip("/)").replace("\\", "/")
    return "/tmp/unknown-opencode-workspace"


def tool_call(name: str, arguments: dict[str, Any], index: int) -> dict[str, Any]:
    call_id = f"smoke-call-{index}"
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class MockProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.issued_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def record(self, path: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append((path, payload))

    def response_plan(self, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        text = request_text(payload)
        results = tool_result_messages(payload)
        if "[smoke_project_read]" in text:
            if not results:
                calls = [
                    tool_call("read", {"filePath": f"{workspace_path(payload)}/README.md"}, 20),
                ]
                self.issued_tool_calls.extend(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                    for call in calls
                )
                return "tools", calls
            return "text", []
        if "[smoke_readonly]" in text:
            if not results:
                calls = [
                    tool_call("read", {"filePath": "README.md"}, 1),
                    tool_call("bash", {"command": "git status --short"}, 2),
                    tool_call(
                        "task",
                        {
                            "description": "forbidden smoke subagent",
                            "prompt": "Do not run; this task tool must be unavailable.",
                            "subagent_type": "general",
                        },
                        7,
                    ),
                    tool_call("webfetch", {"url": "http://127.0.0.1:9/forbidden"}, 8),
                    tool_call("websearch", {"query": "forbidden smoke search"}, 9),
                ]
                self.issued_tool_calls.extend(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                    for call in calls
                )
                return "tools", calls
            return "text", []
        if "[smoke_project_apply]" in text or "[smoke_apply]" in text:
            if not results:
                workspace = workspace_path(payload)
                calls = [
                    tool_call("read", {"filePath": f"{workspace}/README.md"}, 6),
                ]
                self.issued_tool_calls.extend(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                    for call in calls
                )
                return "tools", calls
            if len(results) == 1:
                workspace = workspace_path(payload)
                calls = [
                    tool_call(
                        "write",
                        {
                            "filePath": f"{workspace}/README.md",
                            "content": README_AFTER,
                        },
                        3,
                    )
                ]
                self.issued_tool_calls.extend(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                    for call in calls
                )
                return "tools", calls
            if len(results) < 4:
                workspace = workspace_path(payload)
                calls = [
                    tool_call(
                        "write",
                        {
                            "filePath": f"{workspace}/outside.txt",
                            "content": "MODEL MUST NOT PROPAGATE THIS EDIT.\n",
                        },
                        4,
                    ),
                    tool_call(
                        "write",
                        {
                            "filePath": f"{workspace}/.git/HEAD",
                            "content": "MODEL MUST NOT EDIT GIT METADATA.\n",
                        },
                        5,
                    ),
                ]
                self.issued_tool_calls.extend(
                    (call["function"]["name"], json.loads(call["function"]["arguments"]))
                    for call in calls
                )
                return "tools", calls
            return "text", []
        return "text", []

    def chat_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind, calls = self.response_plan(payload)
        if kind == "tools":
            message: dict[str, Any] = {"role": "assistant", "tool_calls": calls}
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "[]"}
            finish_reason = "stop"
        return {
            "id": f"smoke-response-{len(self.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        }

    def chat_stream_chunks(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        kind, calls = self.response_plan(payload)
        if kind == "tools":
            delta: dict[str, Any] = {"role": "assistant", "tool_calls": calls}
            finish_reason = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "[]"}
            finish_reason = "stop"
        return [
            {
                "id": f"smoke-response-{len(self.requests)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
        ]

    def responses_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind, calls = self.response_plan(payload)
        if kind == "tools":
            output = [
                {
                    "type": "function_call",
                    "id": call["id"],
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
                for call in calls
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "[]"}],
                }
            ]
        return {
            "id": f"smoke-response-{len(self.requests)}",
            "object": "response",
            "created_at": int(time.time()),
            "model": "mock-model",
            "status": "completed",
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            "output": output,
        }

    def responses_stream_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.responses_response(payload)
        response_id = response["id"]
        events: list[dict[str, Any]] = [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            }
        ]
        for output_index, item in enumerate(response["output"]):
            if item["type"] == "function_call":
                partial = {**item, "arguments": "", "status": "in_progress"}
                finished_item = {**item, "status": "completed"}
                events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": partial,
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "response_id": response_id,
                            "item_id": item["id"],
                            "output_index": output_index,
                            "delta": item["arguments"],
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            "response_id": response_id,
                            "item_id": item["id"],
                            "output_index": output_index,
                            "arguments": item["arguments"],
                        },
                        {
                            "type": "response.output_item.done",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": finished_item,
                        },
                    ]
                )
            else:
                message = {
                    "id": f"smoke-message-{len(self.requests)}",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
                content_part = {"type": "output_text", "text": "", "annotations": []}
                finished_part = {"type": "output_text", "text": "[]", "annotations": []}
                finished_message = {**message, "status": "completed", "content": [finished_part]}
                events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": message,
                        },
                        {
                            "type": "response.content_part.added",
                            "response_id": response_id,
                            "item_id": message["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "part": content_part,
                        },
                        {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "item_id": message["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "delta": "[]",
                        },
                        {
                            "type": "response.output_text.done",
                            "response_id": response_id,
                            "item_id": message["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "text": "[]",
                        },
                        {
                            "type": "response.content_part.done",
                            "response_id": response_id,
                            "item_id": message["id"],
                            "output_index": output_index,
                            "content_index": 0,
                            "part": finished_part,
                        },
                        {
                            "type": "response.output_item.done",
                            "response_id": response_id,
                            "output_index": output_index,
                            "item": finished_message,
                        },
                    ]
                )
        events.append({"type": "response.completed", "response": response})
        return events


class MockHandler(http.server.BaseHTTPRequestHandler):
    server: "MockServer"
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"/v1/models", "/models"}:
            self.send_json({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body is not an object")
            self.server.provider.record(self.path, payload)
            if self.path.rstrip("/").endswith("/responses"):
                if payload.get("stream"):
                    self.send_sse(self.server.provider.responses_stream_events(payload))
                    return
                response = self.server.provider.responses_response(payload)
            elif payload.get("stream"):
                self.send_sse(self.server.provider.chat_stream_chunks(payload))
                return
            else:
                response = self.server.provider.chat_response(payload)
            self.send_json(response)
        except Exception as exc:  # pragma: no cover - surfaced by the client
            self.send_json({"error": {"message": str(exc), "type": "mock_error"}}, status=500)

    def send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_sse(self, payloads: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for payload in payloads:
            event_type = str(payload.get("type", "message"))
            self.wfile.write(
                b"event: " + event_type.encode("utf-8") + b"\n"
                + b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
            )
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


class MockServer(http.server.ThreadingHTTPServer):
    def __init__(self, provider: MockProvider) -> None:
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.provider = provider


def protected_git_snapshot(git_dir: pathlib.Path) -> dict[str, tuple[str, int, bytes | str]]:
    snapshot: dict[str, tuple[str, int, bytes | str]] = {}
    for path in sorted(git_dir.rglob("*")):
        relative = path.relative_to(git_dir)
        if relative.parts and relative.parts[0] == "ai-push-hooks":
            continue
        metadata = path.lstat()
        key = relative.as_posix()
        mode = metadata.st_mode & 0o7777
        if path.is_symlink():
            snapshot[key] = ("symlink", mode, os.readlink(path))
        elif path.is_file():
            snapshot[key] = ("file", mode, path.read_bytes())
        elif path.is_dir():
            snapshot[key] = ("directory", mode, b"")
    return snapshot


def create_synthetic_repo(root: pathlib.Path) -> pathlib.Path:
    repo = root / "synthetic-repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    write(repo / "README.md", README_BEFORE)
    write(repo / "outside.txt", "Outside allowlist must remain unchanged.\n")
    write(
        repo / "ai-push-hooks.toml",
        '''[general]
skip_on_sync_branch = false
base_branch = "main"

[llm]
model = "openai/gpt-4o-mini"
timeout_seconds = 60
max_parallel = 1
json_max_retries = 0
delete_session_after_run = true

[logging]
capture_llm_transcript = false
print_llm_output = false

[workflow]
modules = ["smoke"]

[modules.smoke]
enabled = true

[[modules.smoke.steps]]
id = "readonly"
type = "llm"
prompt = "[SMOKE_READONLY] Return an empty JSON array after the tool requests."
output = "readonly.json"
schema = "string_array"

[[modules.smoke.steps]]
id = "apply"
type = "apply"
prompt = "[SMOKE_APPLY] Make only the requested synthetic README edit."
allow_paths = ["README.md"]
''',
    )
    git(repo, "config", "user.name", "OpenCode smoke test")
    git(repo, "config", "user.email", "smoke@example.invalid")
    git(repo, "add", "README.md", "outside.txt", "ai-push-hooks.toml")
    git(repo, "commit", "-m", "synthetic baseline")
    write(repo / "trigger.txt", "This synthetic change triggers the beta gate.\n")
    git(repo, "add", "trigger.txt")
    git(repo, "commit", "-m", "synthetic trigger")
    git(repo, "rev-parse", "HEAD")
    return repo


def assert_provider_contract(provider: MockProvider) -> None:
    readonly_requests = [
        payload for _path, payload in provider.requests if "[smoke_readonly]" in request_text(payload)
    ]
    apply_requests = [
        payload for _path, payload in provider.requests if "[smoke_apply]" in request_text(payload)
    ]
    if not readonly_requests or not apply_requests:
        raise AssertionError("real OpenCode did not reach both mock-provider smoke stages")

    def result_call_id(result: dict[str, Any]) -> str | None:
        for key in ("call_id", "tool_call_id", "toolCallId"):
            value = result.get(key)
            if isinstance(value, str):
                return value
        content = result.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                for key in ("call_id", "tool_call_id", "toolCallId"):
                    value = part.get(key)
                    if isinstance(value, str):
                        return value
        return None

    def results_by_call_id(requests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for payload in requests:
            for result in tool_result_messages(payload):
                call_id = result_call_id(result)
                if call_id:
                    results[call_id] = result
        return results

    def is_denied(result: dict[str, Any]) -> bool:
        text = request_text(result)
        return any(
            word in text
            for word in (
                "denied",
                "permission",
                "not allowed",
                "unavailable",
                "invalid",
                "not found",
                "unknown tool",
                "prevents",
            )
        )

    readonly_results = results_by_call_id(readonly_requests)
    readonly_calls = {
        "smoke-call-1": "read",
        "smoke-call-2": "bash",
        "smoke-call-7": "task",
        "smoke-call-8": "webfetch",
        "smoke-call-9": "websearch",
    }
    for call_id, name in readonly_calls.items():
        result = readonly_results.get(call_id)
        if result is None or not is_denied(result):
            raise AssertionError(f"real OpenCode did not deny or hide read-only `{name}` tool request")

    apply_calls = [
        arguments
        for name, arguments in provider.issued_tool_calls
        if name == "write" and "filePath" in arguments
    ]
    apply_paths = {str(arguments["filePath"]).lower() for arguments in apply_calls}
    if not any(path.endswith("/readme.md") for path in apply_paths):
        raise AssertionError("mock provider did not issue the allowlisted README write request")
    if not any(path.endswith("/outside.txt") for path in apply_paths):
        raise AssertionError("mock provider did not issue the outside-allowlist write request")
    if not any(path.endswith("/.git/head") for path in apply_paths):
        raise AssertionError("mock provider did not issue the protected Git metadata write request")

    apply_results = results_by_call_id(apply_requests)
    allowed_result = apply_results.get("smoke-call-3")
    if allowed_result is None or is_denied(allowed_result):
        raise AssertionError("real OpenCode did not allow the allowlisted README write")
    for call_id, name in {
        "smoke-call-4": "outside-allowlist write",
        "smoke-call-5": "protected Git metadata write",
    }.items():
        result = apply_results.get(call_id)
        if result is None or not is_denied(result):
            raise AssertionError(f"real OpenCode did not deny the {name}")


def run_adapter_project_contract(root: pathlib.Path, repo: pathlib.Path, provider: MockProvider) -> None:
    """Exercise the new runner directly against the same loopback provider.

    The workflow smoke above intentionally preserves the shipped compatibility
    path.  These two calls prove the opt-in project policy independently until
    ST-3 wires runner dispatch into workflow orchestration.
    """

    package_src = pathlib.Path(__file__).resolve().parents[1] / "src"
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))

    from ai_push_hooks.config import load_config
    from ai_push_hooks.executors.runners import RunnerRequest, get_runner
    from ai_push_hooks.types import HookLogger, RuntimeContext

    config, _ = load_config(repo)
    run_dir = root / "adapter-run"
    run_dir.mkdir()
    context = RuntimeContext(
        repo_root=repo,
        git_dir=repo / ".git",
        config=config,
        logger=HookLogger(None),
        remote_name="origin",
        remote_url="loopback://synthetic",
        stdin_lines=[],
        run_id="adapter-contract",
        run_dir=run_dir,
        opencode_executable=shutil.which("opencode"),
    )
    runner = get_runner("opencode")

    read_request = RunnerRequest(
        profile_id="opencode-project",
        runner_type="opencode",
        stage="adapter.project-read",
        purpose="llm:project-read",
        mode="llm",
        instruction=f"[SMOKE_PROJECT_READ] Working directory: {repo}",
        cwd=repo,
        timeout_seconds=60,
        model=config.llm.model,
        project_access="project",
        integration_context=context,
    )
    requests_before_read = len(provider.requests)
    try:
        read_result = runner.run(read_request)
    except Exception as exc:  # pragma: no cover - smoke diagnostics
        seen = " | ".join(
            f"{path} marker={'[smoke_project_read]' in request_text(payload)} "
            f"keys={sorted(payload)} results={len(tool_result_messages(payload))} "
            f"text={request_text(payload)[:180]}"
            for path, payload in provider.requests[requests_before_read:]
        )
        raise RuntimeError(f"adapter project read failed: {exc}; provider requests: {seen}") from exc
    if read_result.final_text.strip() != "[]":
        raise AssertionError("OpenCode project analysis did not return the expected mock response")
    read_requests = [
        payload
        for _path, payload in provider.requests
        if "[smoke_project_read]" in request_text(payload)
    ]
    if not read_requests:
        raise AssertionError("adapter project analysis did not reach the mock provider")
    read_results = tool_result_messages(read_requests[-1])
    if not read_results or any("denied" in request_text(item) for item in read_results):
        raise AssertionError("adapter project analysis read permission was blocked")
    read_result = runner.finalize(read_request, read_result)
    if read_result.session is None or read_result.session.state != "deleted":
        raise AssertionError("adapter project analysis session was not deleted in its isolated environment")

    staging = root / "adapter-project-staging"
    staging.mkdir()
    shutil.copy2(repo / "README.md", staging / "README.md")
    shutil.copy2(repo / "outside.txt", staging / "outside.txt")
    apply_request = RunnerRequest(
        profile_id="opencode-project",
        runner_type="opencode",
        stage="adapter.project-apply",
        purpose="apply:project-apply",
        mode="apply",
        instruction=f"[SMOKE_PROJECT_APPLY] Working directory: {staging}",
        cwd=staging,
        timeout_seconds=60,
        model=config.llm.model,
        project_access="project",
        allow_paths=("README.md",),
        integration_context=context,
    )
    apply_result = runner.run(apply_request)
    apply_requests = [
        payload
        for _path, payload in provider.requests
        if "[smoke_project_apply]" in request_text(payload)
    ]
    if not apply_requests:
        raise AssertionError("adapter project apply did not reach the mock provider")
    apply_results = tool_result_messages(apply_requests[-1])
    if not apply_results or any("denied" in request_text(item) for item in apply_results[:1]):
        raise AssertionError("adapter project apply broad read permission was blocked")
    if (staging / "README.md").read_text(encoding="utf-8") != README_AFTER:
        raise AssertionError("adapter project apply did not update the allowlisted staging file")
    apply_result = runner.finalize(apply_request, apply_result)
    if apply_result.session is None or apply_result.session.state != "deleted":
        raise AssertionError("adapter project apply session was not deleted in its isolated environment")


def main() -> int:
    version = run(["opencode", "--version"]).strip()
    if version != EXPECTED_OPENCODE_VERSION:
        raise RuntimeError(
            f"unexpected installed OpenCode version: {version!r}; expected {EXPECTED_OPENCODE_VERSION}"
        )
    print(f"OpenCode version: {version}")

    provider = MockProvider()
    server = MockServer(provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with tempfile.TemporaryDirectory(prefix="opencode-contract-") as temporary_directory:
            root = pathlib.Path(temporary_directory)
            repo = create_synthetic_repo(root)
            before_git = protected_git_snapshot(repo / ".git")
            before_outside = (repo / "outside.txt").read_bytes()
            head = git(repo, "rev-parse", "HEAD")
            push_input = f"HEAD {head} refs/heads/feature/smoke {'0' * 40}\n"
            package_src = pathlib.Path(__file__).resolve().parents[1] / "src"

            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "XDG_STATE_HOME": str(root / "xdg-state"),
                "XDG_DATA_HOME": str(root / "xdg-data"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONPATH": str(package_src),
                "OPENAI_API_KEY": "synthetic-loopback-only",
                "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            }
            completed = subprocess.run(
                [sys.executable, "-m", "ai_push_hooks", "hook", "origin", "loopback://synthetic"],
                cwd=repo,
                input=push_input,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
                env=env,
            )
            if completed.returncode:
                requests_seen = ", ".join(
                    f"{path} stream={bool(payload.get('stream'))} keys={sorted(payload)}"
                    for path, payload in provider.requests
                )
                raise RuntimeError(
                    f"ai-push-hooks hook failed ({completed.returncode})\n"
                    f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}\n"
                    f"mock provider requests: {requests_seen}"
            )

            if (repo / "README.md").read_text(encoding="utf-8") != README_AFTER:
                request_summary = " | ".join(
                    f"{path}: readonly={'[smoke_readonly]' in request_text(payload)} "
                    f"apply={'[smoke_apply]' in request_text(payload)} "
                    f"results={len(tool_result_messages(payload))}"
                    for path, payload in provider.requests
                )
                apply_summary = " | ".join(
                    json.dumps(tool_result_messages(payload), ensure_ascii=True)[:1600]
                    for _path, payload in provider.requests
                    if "[smoke_apply]" in request_text(payload)
                )
                raise AssertionError(
                    "allowlisted README edit was not propagated; hook stderr: "
                    + completed.stderr
                    + "; provider requests: "
                    + request_summary
                    + "; apply detail: "
                    + apply_summary
                )
            if (repo / "outside.txt").read_bytes() != before_outside:
                raise AssertionError("outside-allowlist content changed")
            status = run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], repo
            ).rstrip("\n")
            if status != " M README.md":
                raise AssertionError(f"unexpected worktree propagation: {status!r}")
            after_git = protected_git_snapshot(repo / ".git")
            if before_git != after_git:
                changed = sorted(set(before_git) ^ set(after_git))
                changed.extend(
                    key for key in sorted(set(before_git) & set(after_git)) if before_git[key] != after_git[key]
                )
                raise AssertionError("protected Git metadata changed: " + ", ".join(changed))

            saved_openai_key = os.environ.get("OPENAI_API_KEY")
            saved_openai_base_url = os.environ.get("OPENAI_BASE_URL")
            saved_xdg_data_home = os.environ.get("XDG_DATA_HOME")
            saved_git_config_nosystem = os.environ.get("GIT_CONFIG_NOSYSTEM")
            os.environ["OPENAI_API_KEY"] = "synthetic-loopback-only"
            os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
            os.environ["XDG_DATA_HOME"] = str(root / "xdg-data")
            os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
            try:
                run_adapter_project_contract(root, repo, provider)
            finally:
                if saved_openai_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = saved_openai_key
                if saved_openai_base_url is None:
                    os.environ.pop("OPENAI_BASE_URL", None)
                else:
                    os.environ["OPENAI_BASE_URL"] = saved_openai_base_url
                if saved_xdg_data_home is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = saved_xdg_data_home
                if saved_git_config_nosystem is None:
                    os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
                else:
                    os.environ["GIT_CONFIG_NOSYSTEM"] = saved_git_config_nosystem

        assert_provider_contract(provider)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print("PASS: real OpenCode contract, allowlist propagation, and Git metadata checks")
    print("PASS: no external model calls (runtime network=none; provider=loopback mock)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
