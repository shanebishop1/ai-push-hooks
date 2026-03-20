from __future__ import annotations

DOCS_QUERY_PROMPT = """Given the attached diff and changed file list, output a JSON array of concise documentation search queries.

Requirements:
- Return JSON only.
- Prefer exact config keys, commands, flags, function names, and domain terms.
- Keep the list concise and unique.
"""

DOCS_ANALYSIS_PROMPT = """Review the attached diff and documentation context. Return JSON issues only for factual documentation drift caused by the code changes.

Each item must include:
- file
- line
- description
- doc_excerpt
- suggested_fix

Return [] when the docs remain factually correct.
"""

DOCS_APPLY_PROMPT = """Apply the minimum Markdown documentation changes required to fix the detected factual drift.

Rules:
1. Modify only README.md and docs/**/*.md files allowed by this step.
2. Keep edits minimal and factual.
3. Update docs index files when a referenced Markdown file is added or renamed.
4. If no edits are required, do not modify files.
"""

BEADS_PLAN_PROMPT = """Check the attached branch context and output a JSON object describing Beads alignment work.

Return keys:
- commands: array of non-interactive br command strings to run
- unresolved: boolean
- report_markdown: markdown string or empty

Return JSON only.
"""

PR_COMPOSE_PROMPT = """Draft a pull request payload from the attached branch context.

Return a JSON object with:
- title
- body
- base_branch
- head_branch
- draft

Return JSON only.
"""

BUILTIN_PROMPTS = {
    "docs-query-basic": DOCS_QUERY_PROMPT,
    "docs-analysis-basic": DOCS_ANALYSIS_PROMPT,
    "docs-apply-basic": DOCS_APPLY_PROMPT,
    "beads-plan-basic": BEADS_PLAN_PROMPT,
    "pr-compose-basic": PR_COMPOSE_PROMPT,
}

MINIMAL_DOCS_TEMPLATE = '''[general]
enabled = true
allow_push_on_error = false
require_clean_worktree = false
skip_on_sync_branch = true

[llm]
runner = "opencode"
model = "openai/gpt-5.3-codex-spark"
variant = ""
timeout_seconds = 800
max_parallel = 2
json_max_retries = 2
invalid_json_feedback_max_chars = 6000
json_retry_new_session = true
delete_session_after_run = true

[logging]
level = "status"
jsonl = true
dir = ".git/ai-push-hooks/logs"
capture_llm_transcript = true
transcript_dir = ".git/ai-push-hooks/transcripts"
summary_dir = ".git/ai-push-hooks/summaries"

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "query"
type = "llm"
prompt = """
Given the attached diff and changed file list, output a JSON array of concise
documentation search queries. Return JSON only.
"""
inputs = ["collect/push.diff", "collect/changed-files.txt"]
output = "queries.json"
schema = "string_array"

[[modules.docs.steps]]
id = "analyze"
type = "llm"
prompt = """
Review the diff and matched docs excerpts. Return JSON issues only for factual
documentation drift caused by the code changes.
"""
inputs = ["collect/push.diff", "collect/docs-context.txt", "query/queries.json", "collect/recent-commits.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = """
Apply the minimum Markdown documentation changes required to fix the detected
factual drift. Modify only files allowed by the step.
"""
inputs = ["collect/push.diff", "collect/docs-context.txt", "analyze/issues.json"]
allow_paths = ["README.md", "docs/**/*.md"]

[[modules.docs.steps]]
id = "assert"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
'''
