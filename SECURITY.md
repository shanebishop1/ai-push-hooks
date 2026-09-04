# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Report it privately with a [GitHub Security Advisory](https://github.com/shanebishop1/ai-push-hooks/security/advisories/new), including affected versions, impact, reproduction details, and any suggested mitigation. Remove credentials, proprietary source, and unneeded transcript content. Maintainers will acknowledge the report, investigate it, and coordinate disclosure and a fix through the advisory. Only the latest release is actively supported with security fixes.

## Threat model and data handling

ai-push-hooks treats repository content, Git paths and metadata, configuration, model output, and concurrent local filesystem changes as potentially unsafe. Its controls constrain model-visible inputs and apply destinations, protect Git metadata and instruction files, validate filesystem state before propagation, and fail closed by default. They are designed to prevent accidental or model-directed changes outside configured boundaries, not to protect against a malicious user or process with the same operating-system permissions.

OpenCode is a separate local process and communicates with the model provider selected in `[llm].model`. Diffs, changed-file context, prompts, and step artifacts can therefore leave the machine under that provider's terms. ai-push-hooks forwards recognized provider credential environment variables and retains OpenCode's existing authentication data directory, but disables sharing and does not inherit project/global plugins, MCP servers, instructions, or custom-provider configuration. Do not commit secrets, and review provider retention and privacy policies before use on sensitive repositories.

Hook logs, summaries, run artifacts, and transcripts are stored locally under `.git/ai-push-hooks/` with private runtime permissions. Transcript capture defaults to **on** at `.git/ai-push-hooks/transcripts`; set `logging.capture_llm_transcript = false` to disable it. OpenCode session deletion defaults to on, but provider-side retention is controlled by the provider.

## Sandbox limitation

OpenCode permissions and temporary-workspace isolation are **not an operating-system sandbox**. The process retains the invoking user's OS-level access, and bounded snapshots cannot observe every ignored path, Git object/LFS store, shared reflog, other linked-worktree metadata, or race with an independent local process. Use an OS sandbox, container, VM, or dedicated low-privilege account when stronger isolation is required. See the README's [OpenCode isolation limits](README.md#opencode-isolation-limits) for the detailed guarantees and exclusions.
