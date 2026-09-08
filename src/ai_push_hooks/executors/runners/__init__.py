"""Internal runner boundary used by future workflow integrations.

Adapters are deliberately not imported here.  ST-2 supplies the four lazy
adapter modules while ST-3 supplies workflow dispatch.  Importing this package
is therefore safe in installations that do not have any selected CLI.
"""

from .contracts import (
    MAX_DIAGNOSTIC_CHARS,
    Runner,
    RunnerAdapterUnavailableError,
    RunnerArtifact,
    RunnerCapabilities,
    RunnerContractError,
    RunnerError,
    RunnerExecutableNotFoundError,
    RunnerMissingOutputError,
    RunnerNonzeroExitError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    RunnerSignalError,
    RunnerTimeoutError,
    SessionMetadata,
    PromptPacket,
    build_prompt_packet,
    bounded_diagnostic,
    bounded_redacted_diagnostics,
    finalize_runner,
    redact_diagnostic,
    require_final_text,
    require_zero_exit,
    strip_terminal_controls,
)
from .process import DEFAULT_MAX_OUTPUT_BYTES, ProcessResult, run_process
from .registry import (
    DEFAULT_RUNNER_REGISTRY,
    KNOWN_RUNNER_TYPES,
    LazyRunnerSpec,
    RunnerRegistry,
    get_runner,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_RUNNER_REGISTRY",
    "KNOWN_RUNNER_TYPES",
    "MAX_DIAGNOSTIC_CHARS",
    "LazyRunnerSpec",
    "ProcessResult",
    "PromptPacket",
    "Runner",
    "RunnerAdapterUnavailableError",
    "RunnerArtifact",
    "RunnerCapabilities",
    "RunnerContractError",
    "RunnerError",
    "RunnerExecutableNotFoundError",
    "RunnerMissingOutputError",
    "RunnerNonzeroExitError",
    "RunnerProtocolError",
    "RunnerRegistry",
    "RunnerRequest",
    "RunnerResult",
    "RunnerSignalError",
    "RunnerTimeoutError",
    "SessionMetadata",
    "bounded_diagnostic",
    "bounded_redacted_diagnostics",
    "build_prompt_packet",
    "finalize_runner",
    "get_runner",
    "redact_diagnostic",
    "require_final_text",
    "require_zero_exit",
    "run_process",
    "strip_terminal_controls",
]
