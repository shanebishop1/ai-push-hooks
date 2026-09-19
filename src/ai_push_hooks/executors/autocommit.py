"""Opt-in commit of the edits an apply step propagated.

The apply runner cannot do this itself, by construction: staging excludes `.git`,
so the runner has no repository to commit to, and post-propagation verification
fails the step if the checkout's index or Git metadata moved while it ran. The
commit is therefore made here, by the host, after propagation has been verified
-- deterministic, bounded to the files apply actually changed, and independent of
whether the runner claims to have done anything.

Off unless `auto_commit` is set; `auto_push` additionally sends the commit.

Either way the push already in flight is stopped (see `PushSupersededError`):
Git scoped it to the pre-fix commit before the hook ran, so it cannot carry the
fix, and letting it through would ship the unfixed code while reporting success.
With `auto_push` the fix is already on the remote and the developer does nothing
further; without it they re-run `git push`. Because Git collapses every nonzero
pre-push hook exit to `1`, the outcome is reported to non-interactive callers
through `PUSHED_NOTICE` / `RETRY_NOTICE` rather than exit status.
"""

from __future__ import annotations

import os
import re
import sys

from ..config import validate_commit_subject
from ..git_utils import run_command
from ..types import HookError, RuntimeContext, StepConfig

COMMIT_DIRECTIVE_PATTERN = re.compile(r"^\s*COMMIT:\s*(.+?)\s*$", re.MULTILINE)
# The final line of a superseded-push message. Readable as a sentence, and stable
# enough to match on, because Git gives non-interactive callers no usable exit
# status. Treat both phrases as a contract: reword the detail after them freely,
# never the phrases themselves.
PUSHED_NOTICE = "ai-push-hooks Push Success"
RETRY_NOTICE = "ai-push-hooks Push Again"
DEFAULT_COMMIT_SUBJECT = "fix: apply automated review fixes"
COMMIT_TRAILER = "Committed-By: ai-push-hooks"


def commit_subject_from_response(response: str | None) -> str | None:
    """Return the subject the apply runner proposed, or None if unusable.

    The runner is asked to end its response with a single `COMMIT: <subject>`
    line. Anything it sends is untrusted text, so the last match wins and it is
    held to the same rules as a hand-written subject; a malformed proposal is
    discarded in favour of the deterministic default rather than failing the run.
    """

    if not response:
        return None
    matches = COMMIT_DIRECTIVE_PATTERN.findall(response)
    if not matches:
        return None
    try:
        return validate_commit_subject(matches[-1], "Runner-authored commit subject")
    except HookError:
        return None


def build_commit_message(subject: str, changed_files: list[str], step_id: str) -> str:
    """Compose the full commit message from a validated subject."""

    body = "\n".join(f"- {name}" for name in changed_files)
    return (
        f"{subject}\n\n"
        f"Applied by the `{step_id}` apply step against the outgoing diff.\n\n"
        f"{body}\n\n"
        f"{COMMIT_TRAILER}\n"
    )


PENDING_CACHE_KEY = "pending_auto_commits"


def register_pending_auto_commit(
    context: RuntimeContext,
    step: StepConfig,
    changed_files: list[str],
    runner_response: str | None,
    preexisting_dirty: list[str] | None = None,
) -> None:
    """Record edits to be committed once the rest of the workflow has passed.

    Deferred rather than committed on the spot so that later steps -- tests, a
    deterministic postcondition, an assert gate -- retain the ability to reject
    the edits. Nothing is committed if any of them fails.
    """

    pending = context.cache.setdefault(PENDING_CACHE_KEY, [])
    pending.append(
        {
            "step": step,
            "changed_files": list(changed_files),
            "runner_response": runner_response,
            "preexisting_dirty": list(preexisting_dirty or []),
        }
    )


def finalize_pending_auto_commits(context: RuntimeContext) -> list[dict[str, object]]:
    """Commit everything registered during a run that has now fully passed."""

    pending = context.cache.get(PENDING_CACHE_KEY) or []
    outcomes = [
        run_auto_commit(
            context,
            record["step"],
            record["changed_files"],
            record["runner_response"],
            record.get("preexisting_dirty") or [],
        )
        for record in pending
    ]
    context.cache[PENDING_CACHE_KEY] = []
    return outcomes


def _superseded_commit(context: RuntimeContext) -> str:
    """Return the commit the developer's own `git push` was scoped to."""

    updates = context.cache.get("pushed_branch_updates") or []
    return str(updates[0].local_sha) if len(updates) == 1 else ""


def _resolve_push_target(context: RuntimeContext) -> tuple[str, str]:
    """Return the (remote, remote_ref) the superseded push was aimed at."""

    updates = context.cache.get("pushed_branch_updates") or []
    if len(updates) != 1:
        raise HookError(
            "auto_push requires exactly one non-deletion pushed branch update"
        )
    remote = (context.remote_name or "").strip()
    if not remote:
        raise HookError("auto_push requires a remote name from the pre-push hook")
    remote_ref = updates[0].remote_ref
    if not remote_ref.startswith("refs/heads/"):
        raise HookError(f"auto_push refusing non-branch ref: {remote_ref}")
    return remote, remote_ref


def run_auto_commit(
    context: RuntimeContext,
    step: StepConfig,
    changed_files: list[str],
    runner_response: str | None,
    preexisting_dirty: list[str] | None = None,
) -> dict[str, object]:
    """Commit the propagated files and report what was committed."""

    if not changed_files:
        return {"committed": False, "reason": "no propagated changes"}

    # `git commit -- <file>` commits the whole file, not just the fix. If the
    # developer already had uncommitted work in one of these files, committing it
    # would capture work they never staged -- and with auto_push, publish it.
    if preexisting_dirty:
        return {
            "committed": False,
            "blocked": True,
            "preexisting_dirty": list(preexisting_dirty),
            "reason": "files had uncommitted changes before this run",
        }

    subject = step.commit_message or commit_subject_from_response(runner_response)
    subject_source = "config" if step.commit_message else "runner"
    if subject is None:
        subject, subject_source = DEFAULT_COMMIT_SUBJECT, "default"

    message = build_commit_message(subject, changed_files, step.id)

    # Pathspec-limited: a dirty file the apply step did not touch is never swept
    # into this commit. `--` keeps a path that looks like an option out of argv.
    run_command(
        ["git", "commit", "--no-verify", "-m", message, "--", *changed_files],
        cwd=context.repo_root,
        check=True,
    )
    commit_sha = run_command(
        ["git", "rev-parse", "HEAD"],
        cwd=context.repo_root,
        check=True,
    ).stdout.strip()
    context.logger.info(
        "apply.auto_commit",
        f"Committed {len(changed_files)} file(s) as {commit_sha[:12]}",
        step=step.id,
        subject_source=subject_source,
    )

    outcome: dict[str, object] = {
        "committed": True,
        "commit": commit_sha,
        "subject": subject,
        "subject_source": subject_source,
        "committed_files": list(changed_files),
        "superseded_commit": _superseded_commit(context),
        "pushed": False,
    }
    if not step.auto_push:
        return outcome

    remote, remote_ref = _resolve_push_target(context)
    # The nested push must not re-enter this hook. `--no-verify` skips it, and the
    # environment flag is a second, independent stop in case a hook manager wires
    # the hook somewhere `--no-verify` does not reach.
    push = run_command(
        ["git", "push", "--no-verify", remote, f"{commit_sha}:{remote_ref}"],
        cwd=context.repo_root,
        check=False,
        env={"AI_PUSH_HOOKS_SKIP": "1"},
    )
    outcome["push_remote"] = remote
    outcome["push_ref"] = remote_ref
    if push.returncode != 0:
        detail = (push.stderr.strip() or push.stdout.strip() or "unknown error").strip()
        outcome["push_error"] = detail
        context.logger.warn(
            "apply.auto_push", "Nested push failed", step=step.id, detail=detail
        )
        return outcome

    outcome["pushed"] = True
    context.logger.info(
        "apply.auto_push",
        f"Pushed {commit_sha[:12]} to {remote} {remote_ref}",
        step=step.id,
    )
    return outcome


def _supports_color() -> bool:
    """Colour only a real terminal, and never when NO_COLOR is set."""

    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        return False


def _paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _supports_color() else text


def combined_superseded_message(outcomes: list[dict[str, object]]) -> str:
    """Render the outcome as a numbered push ledger.

    The `git push` the developer ran is push 1 and it always fails: Git scoped it
    to the pre-fix commit before the hook ran. With `auto_push`, push 2 is the one
    this hook made, carrying the fix. Showing both numbered is the only way the
    trailing `error: failed to push some refs` reads correctly -- it belongs to
    push 1, not to the fix.

    The last line is a stable marker, because Git collapses every nonzero pre-push
    hook exit to `1` and exit status therefore cannot tell a caller whether to
    retry.
    """

    blocked = [outcome for outcome in outcomes if outcome.get("blocked")]
    if blocked:
        files = "\n".join(
            f"    {name}"
            for outcome in blocked
            for name in outcome.get("preexisting_dirty", [])
        )
        return (
            _paint(
                "Refusing to auto-commit: the fix touched files you had already "
                "edited.",
                "33",
            )
            + "\n\n"
            + f"{files}\n\n"
            + "Committing these would capture uncommitted work you never staged, "
            "so nothing was committed.\nThe fix is applied in your working tree. "
            "Review `git diff`, commit what you want, and push again.\n\n"
            + f"{RETRY_NOTICE} - nothing was committed; review your working tree "
            "first, then run `git push` again."
        )

    committed = [outcome for outcome in outcomes if outcome.get("committed")]
    if not committed:
        return ""

    final = committed[-1]
    pushed = bool(final.get("pushed"))
    superseded = str(final.get("superseded_commit", ""))[:12] or "your commit"

    lines = []
    for outcome in committed:
        commit = str(outcome.get("commit", ""))[:12]
        files = "\n".join(f"    {name}" for name in outcome.get("committed_files", []))
        lines.append(f"  committed {commit}  {outcome.get('subject', '')}\n{files}")
    body = "\n".join(lines)

    ledger = [
        f"  push 1  {superseded}  "
        + _paint("FAILED", "31")
        + "     scoped to the pre-fix commit, superseded",
    ]
    if pushed:
        ledger.append(
            f"  push 2  {str(final.get('commit', ''))[:12]}  "
            + _paint("SUCCEEDED", "32")
            + f"  pushed to {final.get('push_remote')} {final.get('push_ref')}"
        )

    if pushed:
        headline = _paint("The fix is on the remote. Nothing further is needed.", "32")
        footer = (
            "Git prints `error: failed to push some refs` below. That is push 1.\n"
            "Push 2 carried the fix and succeeded."
        )
        commits = ", ".join(str(o.get("commit", ""))[:12] for o in committed)
        # Never coloured: this line is the machine-readable contract, and an
        # escape prefix would break callers that anchor their match to it.
        marker = (
            f"{PUSHED_NOTICE} - {commits} is on {final.get('push_remote')} "
            f"{final.get('push_ref')}. Nothing further to run."
        )
    else:
        reason = final.get("push_error")
        headline = _paint(
            "The fix is committed locally but is NOT on the remote."
            if reason
            else "The fix is committed locally.",
            "33",
        )
        footer = (
            f"auto_push failed: {reason}\n\n" if reason else ""
        ) + "Run `git push` again to send it."
        commits = ", ".join(str(o.get("commit", ""))[:12] for o in committed)
        marker = (
            f"{RETRY_NOTICE} - {commits} is committed locally but not on the "
            "remote. Run `git push` again to send it."
        )

    return f"{headline}\n\n{body}\n\n" + "\n".join(ledger) + f"\n\n{footer}\n\n{marker}"


def superseded_message(outcome: dict[str, object]) -> str:
    """Explain a single commit this run made."""

    commit = str(outcome.get("commit", ""))[:12]
    files = "\n".join(f"  {name}" for name in outcome.get("committed_files", []))
    return f"Committed {commit} ({outcome.get('subject', '')}):\n{files}"
