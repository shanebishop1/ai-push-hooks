from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FULL_OID_LENGTH = 40
REQUIRE_LEFTHOOK_ENV = "AI_PUSH_HOOKS_REQUIRE_LEFTHOOK"


def _require_lefthook() -> bool:
    return os.environ.get(REQUIRE_LEFTHOOK_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _run(
    args: list[str],
    cwd: pathlib.Path,
    env: dict[str, str],
    *,
    check: bool = True,
    timeout: float = 45,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        input=input_text,
        check=check,
        timeout=timeout,
    )


def _git(cwd: pathlib.Path, env: dict[str, str], *args: str) -> str:
    return _run(["git", *args], cwd, env).stdout.strip()


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Installed Hook Test",
            "GIT_AUTHOR_EMAIL": "installed-hook@example.invalid",
            "GIT_COMMITTER_NAME": "Installed Hook Test",
            "GIT_COMMITTER_EMAIL": "installed-hook@example.invalid",
        }
    )
    return env


def _scenario_config(*, reject: bool = False) -> str:
    if reject:
        return """\
[general]
allow_push_on_error = false

[logging]
jsonl = false
capture_llm_transcript = false

[workflow]
modules = ["gate"]

[modules.gate]
enabled = true

[[modules.gate.steps]]
id = "reject"
type = "assert"
python = "checks/hooks.py:reject"
"""
    return """\
[general]
allow_push_on_error = false

[logging]
jsonl = false
capture_llm_transcript = false

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
python = "checks/hooks.py:collect_context"

[[modules.docs.steps]]
id = "command"
type = "exec"
command = ["{python}", "checks/installed_command.py"]
inputs = ["collect/context.json"]
stdin = "collect/context.json"

[[modules.docs.steps]]
id = "policy"
type = "assert"
python = "checks/hooks.py:assert_context"
inputs = ["command/result.json"]
"""


def _disabled_config() -> str:
    return """\
[general]
enabled = false

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
python = "checks/hooks.py:should_not_run"
"""


def _apply_config() -> str:
    return """\
[general]
allow_push_on_error = false

[llm]
delete_session_after_run = true

[logging]
jsonl = false
capture_llm_transcript = false

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "apply"
type = "apply"
fallback_prompt_id = "docs-apply-basic"
inputs = ["collect/push.diff"]
allow_paths = ["README.md"]

[[modules.docs.steps]]
id = "assert"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
"""


@pytest.fixture(scope="session")
def installed_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, pathlib.Path]:
    configured = {
        "wheel": os.environ.get("AI_PUSH_HOOKS_RELEASE_WHEEL"),
        "npm": os.environ.get("AI_PUSH_HOOKS_RELEASE_NPM_TARBALL"),
    }
    if any(configured.values()):
        if not all(configured.values()):
            raise AssertionError("both exact release artifact paths must be provided")
        artifacts = {kind: pathlib.Path(path).resolve() for kind, path in configured.items()}
        assert artifacts["wheel"].is_file() and artifacts["wheel"].suffix == ".whl"
        assert artifacts["npm"].is_file() and artifacts["npm"].suffix == ".tgz"
        return artifacts

    output = tmp_path_factory.mktemp("installed-artifacts")
    wheel_dir = output / "wheel"
    npm_dir = output / "npm"
    wheel_dir.mkdir()
    npm_dir.mkdir()
    _run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        REPO_ROOT,
        _isolated_env(),
        timeout=120,
    )
    wheels = sorted(wheel_dir.glob("ai_push_hooks-*.whl"))
    assert len(wheels) == 1
    packed = _run(
        ["npm", "pack", "--json", "--pack-destination", str(npm_dir)],
        REPO_ROOT,
        _isolated_env(),
        timeout=120,
    )
    package_metadata = json.loads(packed.stdout)
    tarball = npm_dir / package_metadata[0]["filename"]
    assert tarball.is_file()
    return {"wheel": wheels[0], "npm": tarball}


def _install_repo_fixtures(repo: pathlib.Path) -> None:
    checks = repo / "checks"
    checks.mkdir()
    fixture_root = pathlib.Path(__file__).parent / "fixtures"
    shutil.copyfile(fixture_root / "integration_hooks.py", checks / "hooks.py")
    shutil.copyfile(fixture_root / "installed_command.py", checks / "installed_command.py")


def _latest_run(repo: pathlib.Path) -> pathlib.Path:
    runs = sorted((repo / ".git" / "ai-push-hooks" / "runs").iterdir())
    assert runs
    return runs[-1]


def _prepare_wheel_command(
    artifact: pathlib.Path, root: pathlib.Path, env: dict[str, str]
) -> pathlib.Path:
    install_dir = root / "wheel-install"
    bin_dir = root / "wheel-bin"
    install_dir.mkdir()
    bin_dir.mkdir()
    _run(
        [
            "uv",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(install_dir),
            str(artifact),
        ],
        root,
        env,
        timeout=120,
    )
    wrapper = bin_dir / "ai-push-hooks"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(install_dir)!r})\n"
        "from ai_push_hooks.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    module_probe = _run(
        [str(wrapper), "--help"],
        root,
        env,
    ).stdout
    assert "install" in module_probe
    probe_env = {**env, "PYTHONPATH": str(install_dir)}
    module_path = _run(
        [sys.executable, "-c", "import ai_push_hooks; print(ai_push_hooks.__file__)"],
        root,
        probe_env,
    ).stdout.strip()
    assert pathlib.Path(module_path).resolve().is_relative_to(install_dir.resolve())
    return wrapper


def _prepare_npm_command(
    artifact: pathlib.Path, package_dir: pathlib.Path, env: dict[str, str]
) -> pathlib.Path:
    package_dir.mkdir(exist_ok=True)
    _run(["npm", "init", "-y"], package_dir, env)
    _run(
        [
            "npm",
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(artifact),
        ],
        package_dir,
        env,
        timeout=120,
    )
    installed_source = package_dir / "node_modules" / "ai-push-hooks" / "src" / "ai_push_hooks"
    assert installed_source.is_dir()
    command = package_dir / "node_modules" / ".bin" / "ai-push-hooks"
    _run([str(command), "--help"], package_dir, env)
    return command


@pytest.mark.parametrize("distribution", ["wheel", "npm"])
def test_installed_hook_runs_real_local_push_scenario(
    distribution: str,
    installed_artifacts: dict[str, pathlib.Path],
    tmp_path: pathlib.Path,
) -> None:
    env = _isolated_env()
    root = tmp_path / f"{distribution}-scenario"
    root.mkdir()
    repo = root / "client repo"
    repo.mkdir()
    command = (
        _prepare_wheel_command(installed_artifacts[distribution], root, env)
        if distribution == "wheel"
        else _prepare_npm_command(installed_artifacts[distribution], repo, env)
    )
    command_bin = command.parent
    python_dir = pathlib.Path(sys.executable).parent
    node_dir = pathlib.Path(shutil.which("node") or sys.executable).parent
    git_dir = pathlib.Path(shutil.which("git") or sys.executable).parent
    env["PATH"] = f"{command_bin}:{python_dir}:{node_dir}:{git_dir}:/usr/bin:/bin"

    remote = root / "remote path with spaces.git"
    _run(["git", "init", "--bare", str(remote)], root, env)
    _run(["git", "init", "-b", "main", "."], repo, env)
    _run(["git", "config", "user.name", "Installed Hook Test"], repo, env)
    _run(["git", "config", "user.email", "installed-hook@example.invalid"], repo, env)
    _run(["git", "remote", "add", "origin", str(remote)], repo, env)
    (repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "INDEX.md").write_text("# Docs\n", encoding="utf-8")
    _install_repo_fixtures(repo)
    (repo / "ai-push-hooks.toml").write_text(_scenario_config(), encoding="utf-8")
    if distribution == "npm":
        (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _run(["git", "add", "."], repo, env)
    _run(["git", "commit", "-m", "initial"], repo, env)
    _run(["git", "push", "origin", "main"], repo, env)

    if distribution == "npm":
        _run(["npx", "--no-install", "ai-push-hooks", "install"], repo, env)
    else:
        _run([str(command), "install"], repo, env)
    env["PATH"] = f"{python_dir}:{node_dir}:{git_dir}:/usr/bin:/bin"
    hook_path = pathlib.Path(_git(repo, env, "rev-parse", "--git-path", "hooks")) / "pre-push"
    if not hook_path.is_absolute():
        hook_path = (repo / hook_path).resolve()
    assert hook_path.is_file() and os.access(hook_path, os.X_OK)
    hook_text = hook_path.read_text(encoding="utf-8")
    if distribution == "npm":
        assert "ai-push-hooks.js" in hook_text
        assert "command -v ai-push-hooks" not in hook_text

    baseline_oid = _git(repo, env, "rev-parse", "HEAD")
    remote_baseline_oid = _git(repo, env, "rev-parse", "refs/remotes/origin/main")
    zero_oid = "0" * FULL_OID_LENGTH
    remote_url = _git(repo, env, "config", "--get", "remote.origin.url")

    def invoke(stdin_text: str, *, expected: int, overrides: dict[str, str] | None = None) -> None:
        hook_env = {**env, **(overrides or {})}
        completed = _run(
            [str(hook_path), "origin", remote_url],
            repo,
            hook_env,
            check=False,
            timeout=45,
            input_text=stdin_text,
        )
        assert completed.returncode == expected, completed.stderr

    valid_branch = f"refs/heads/main {baseline_oid} refs/heads/main {remote_baseline_oid}\n"
    invoke("", expected=0)
    collect_report = json.loads(
        (_latest_run(repo) / "docs" / "00-collect" / "context.json").read_text(encoding="utf-8")
    )
    assert collect_report["dependency"] == "installed-interpreter"
    assert pathlib.Path(collect_report["package_origin"]).is_relative_to(
        (root / ("wheel-install" if distribution == "wheel" else "client repo/node_modules/ai-push-hooks/src")).resolve()
    )
    assert (repo / ".git" / "ai-push-hooks" / "runs").is_dir()
    invoke(f"refs/tags/v1 {baseline_oid} refs/tags/v1 {zero_oid}\n", expected=0)
    invoke(valid_branch + f"refs/tags/v1 {baseline_oid} refs/tags/v1 {zero_oid}\n", expected=0)
    invoke(f"refs/tags/v1 {zero_oid} refs/tags/v1 {baseline_oid}\n", expected=0)
    invoke("malformed stdin\n", expected=1)
    invoke(f"refs/heads/main {baseline_oid} refs/heads/main {'a' * FULL_OID_LENGTH}\n", expected=1)
    invoke(
        valid_branch + f"refs/heads/other {baseline_oid} refs/heads/other {zero_oid}\n",
        expected=1,
    )

    (repo / "ai-push-hooks.toml").write_text(_scenario_config(reject=True), encoding="utf-8")
    invoke(valid_branch, expected=1)
    invoke(valid_branch, expected=0, overrides={"AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR": "1"})
    (repo / "ai-push-hooks.toml").write_text(_scenario_config(), encoding="utf-8")
    invoke("malformed stdin\n", expected=0, overrides={"AI_PUSH_HOOKS_SKIP": "1"})
    (repo / "ai-push-hooks.toml").write_text(_disabled_config(), encoding="utf-8")
    invoke("malformed stdin\n", expected=0)
    (repo / "ai-push-hooks.toml").write_text(_scenario_config(), encoding="utf-8")

    fake_bin = root / "fake-tools"
    fake_bin.mkdir()
    fake_opencode = fake_bin / "opencode"
    fake_opencode.write_text(
        "#!/bin/sh\nprintf '# Applied\\n' > README.md\n"
        "printf '%s\\n' '{\"type\":\"text\",\"sessionID\":\"fake-session\",\"part\":{\"text\":\"{}\"}}'\n",
        encoding="utf-8",
    )
    fake_opencode.chmod(0o755)
    apply_env = {**env, "PATH": f"{fake_bin}:{env['PATH']}"}
    (repo / "ai-push-hooks.toml").write_text(_apply_config(), encoding="utf-8")
    invoke(valid_branch, expected=1, overrides={"PATH": apply_env["PATH"]})
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Applied\n"
    _run(["git", "add", "README.md"], repo, apply_env)
    _run(["git", "commit", "-m", "review applied docs"], repo, apply_env)
    reviewed_oid = _git(repo, apply_env, "rev-parse", "HEAD")
    reviewed_branch = f"refs/heads/main {reviewed_oid} refs/heads/main {remote_baseline_oid}\n"
    invoke(reviewed_branch, expected=0, overrides={"PATH": apply_env["PATH"]})
    (repo / "ai-push-hooks.toml").write_text(_scenario_config(), encoding="utf-8")

    (repo / "change.md").write_text("outgoing docs\n", encoding="utf-8")
    _run(["git", "add", "change.md"], repo, env)
    _run(["git", "commit", "-m", "outgoing"], repo, env)
    local_oid = _git(repo, env, "rev-parse", "HEAD")
    remote_oid = _git(repo, env, "rev-parse", "refs/remotes/origin/main")
    assert len(local_oid) == FULL_OID_LENGTH and len(remote_oid) == FULL_OID_LENGTH
    remote_url = _git(repo, env, "config", "--get", "remote.origin.url")
    direct = _run(
        [str(hook_path), "origin", remote_url],
        repo,
        env,
        check=False,
        timeout=45,
        input_text=f"refs/heads/main {local_oid} refs/heads/main {remote_oid}\n",
    )
    assert direct.returncode == 0, direct.stderr
    _run(["git", "push", "origin", "main"], repo, env, timeout=45)
    assert _git(repo, env, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == local_oid

    (repo / "ai-push-hooks.toml").write_text(_scenario_config(reject=True), encoding="utf-8")
    (repo / "rejected.txt").write_text("must not arrive\n", encoding="utf-8")
    _run(["git", "add", "ai-push-hooks.toml", "rejected.txt"], repo, env)
    _run(["git", "commit", "-m", "rejected"], repo, env)
    remote_before_rejection = _git(repo, env, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
    rejected = _run(["git", "push", "origin", "main"], repo, env, check=False, timeout=45)
    assert rejected.returncode != 0
    assert _git(repo, env, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == remote_before_rejection


def test_real_lefthook_install_uses_installed_runner(
    installed_artifacts: dict[str, pathlib.Path], tmp_path: pathlib.Path
) -> None:
    lefthook = shutil.which("lefthook")
    if not lefthook:
        if _require_lefthook():
            pytest.fail("Lefthook is required for the CI installed-hook gate")
        pytest.skip("Lefthook is not installed")
    version = subprocess.run(
        [lefthook, "version"], capture_output=True, text=True, timeout=15
    )
    if version.returncode != 0:
        if _require_lefthook():
            pytest.fail(f"Lefthook is required in CI: {version.stderr.strip()}")
        pytest.skip("Lefthook is unavailable in the local tool environment")

    env = _isolated_env()
    root = tmp_path / "lefthook-scenario"
    root.mkdir()
    command = _prepare_wheel_command(installed_artifacts["wheel"], root, env)
    repo = root / "client repo"
    repo.mkdir()
    remote = root / "remote.git"
    _run(["git", "init", "--bare", str(remote)], root, env)
    _run(["git", "init", "-b", "main", "."], repo, env)
    _run(["git", "config", "user.name", "Installed Hook Test"], repo, env)
    _run(["git", "config", "user.email", "installed-hook@example.invalid"], repo, env)
    _run(["git", "remote", "add", "origin", str(remote)], repo, env)
    (repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "INDEX.md").write_text("# Docs\n", encoding="utf-8")
    (repo / "ai-push-hooks.toml").write_text(_scenario_config(), encoding="utf-8")
    _run(["git", "add", "."], repo, env)
    _run(["git", "commit", "-m", "initial"], repo, env)
    _run(["git", "push", "origin", "main"], repo, env)

    command_text = shlex.quote(str(command))
    (repo / "lefthook.yml").write_text(
        "pre-push:\n"
        "  commands:\n"
        "    installed-runner:\n"
        f"      run: {command_text} hook {{1}} {{2}}\n"
        "      use_stdin: true\n",
        encoding="utf-8",
    )
    _run([lefthook, "install"], repo, env, timeout=45)
    installed_hook = repo / ".git" / "hooks" / "pre-push"
    assert installed_hook.is_file() and os.access(installed_hook, os.X_OK)

    (repo / "change.md").write_text("through lefthook\n", encoding="utf-8")
    _run(["git", "add", "change.md"], repo, env)
    _run(["git", "commit", "-m", "lefthook push"], repo, env)
    _run(["git", "push", "origin", "main"], repo, env, timeout=45)
    assert _git(repo, env, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == _git(
        repo, env, "rev-parse", "HEAD"
    )
