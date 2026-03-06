from __future__ import annotations

from .beads import collect_beads_status_context
from .docs import collect_docs_context
from .pr import collect_pr_context

COLLECTORS = {
    "docs_context": collect_docs_context,
    "beads_status_context": collect_beads_status_context,
    "pr_context": collect_pr_context,
}
