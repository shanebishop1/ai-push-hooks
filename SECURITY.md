# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Report it privately with a [GitHub Security Advisory](https://github.com/shanebishop1/ai-push-hooks/security/advisories/new), including affected versions, impact, reproduction details, and any suggested mitigation. Remove credentials, proprietary source, and unneeded transcript content. Maintainers will acknowledge the report, investigate it, and coordinate disclosure and a fix through the advisory. Only the latest release is actively supported with security fixes.

## Threat model and data handling

ai-push-hooks treats repository content, Git paths and metadata, configuration, model output, and concurrent local filesystem changes as potentially unsafe. Its controls constrain model-visible inputs and apply destinations, protect Git metadata and instruction files, validate filesystem state before propagation, and fail closed by default. They are designed to prevent accidental or model-directed changes outside configured boundaries, not to protect against a malicious user or process with the same operating-system permissions.

Every selected runner is a separate local process. Diffs, changed-file context,
prompts, artifacts, and (in project mode) more repository content can therefore
leave the machine under the selected provider's terms. OpenCode retains its
existing authentication data directory and forwards recognized provider
environment variables, including `OPENAI_API_KEY`; OpenCode itself chooses the
authentication path. Codex, Claude, and custom commands inherit the user's
normal environment/home needed by their tooling. Authentication is user-owned:
ai-push-hooks does not log environment values, manage credentials, invoke login,
or provide a credential broker. Do not commit secrets, and review provider
retention, privacy, and billing policies before use on sensitive repositories.

OpenCode's `--pure` and isolated configuration exclude external plugins,
project/global configuration, MCP servers, instructions, and global custom
providers while retaining built-in plugins such as Codex OAuth. The default
OpenCode profile remains artifact-only; project access is an explicit opt-in.

Hook logs, summaries, run artifacts, and OpenCode transcripts are stored locally
under `.git/ai-push-hooks/` with private runtime permissions. Transcript capture
defaults to **on** at `.git/ai-push-hooks/transcripts`; set
`logging.capture_llm_transcript = false` to disable it. OpenCode session deletion
defaults to on, but provider-side retention is controlled by the provider.
Codex and Claude are ephemeral/no-persistence by default, and command profiles
have no inferred transcript lifecycle.

Transcript export is best effort. If export fails or produces no usable output,
the run emits a warning and still applies the configured session-deletion
policy; it does not claim that a transcript was captured. A provider may have
already received the request even when local export fails. Do not use local
transcript files as proof that provider-side data was deleted.

## Boundary and apply limitations

OpenCode permissions and temporary-workspace isolation are **not an
operating-system sandbox**. There is no mandatory command allowlist, shell
parser, container, credential broker, or trust prompt. Custom commands are
arbitrary user-authorized argv programs and a nominally read-only custom `llm`
profile is not enforced as read-only. `collect`/`llm` work may overlap up to
`max_parallel`; trusted custom commands must tolerate that. `apply` is globally
serialized, but this does not prevent a same-user process from changing the
host.

Project apply uses a point-in-time staging projection. It excludes ignored files
including tracked-but-ignored files, Git metadata, casefolded/Unicode-normalized
`AGENTS.md` paths, symlinks/reparse points, and special files. Propagation still
requires `allow_paths` plus destination and Git-state checks. Runner inputs and
captured stdout/stderr are each bounded to 16 MiB; staging is bounded to 10,000
entries/256 MiB and Git metadata snapshots to 20,000 entries/64 MiB. These
limits are resource and scope controls, not isolation. Existing baseline checks
are not an atomic CAS against arbitrary external writers, and automatic rollback
is avoided to protect pre-existing user changes. See the README's [runner
profiles and access modes](README.md#runner-profiles-and-access-modes).

Timeout cleanup has platform limits: POSIX uses a private process group on a
best-effort basis, while Windows can terminate only the direct child. Neither
is a sandbox; Windows has no native beta evidence.

## Tested security boundary

The final pinned-Lefthook suite passed **407 tests with no skips**. The current
evidence also includes the real OpenCode **1.18.29** contract smoke test with an
in-process loopback mock provider and no external model call; its read probe
checks that no Git-visible project files were mutated. It does not cover every
provider, model, authentication mode, or live `apply` path.
Installed Codex **0.148.0** and Claude **2.1.220** checks use only version/help
output. Live Codex/Pi verification was intentionally not run pending separate
approval; Claude live verification is pending because no subscription is
available. Treat generated-hook path checks and the Lefthook runner as
integration safeguards, not isolation boundaries.
