"""Agent execution configuration (aligned with omicverse agent_config)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class SandboxFallbackPolicy(Enum):
    """What to do when notebook execution fails."""

    RAISE = "raise"
    WARN_AND_FALLBACK = "warn"
    SILENT = "silent"


# Jupyter / Agent code execution ceiling (large downloads, long pipelines).
EXECUTION_TIMEOUT_SEC = 36000  # 10 hours
# Abort when a download file stops growing for this long.
DOWNLOAD_STALL_TIMEOUT_SEC = 180  # 3 minutes
DOWNLOAD_STALL_POLL_SEC = 30


@dataclass
class ExecutionConfig:
    use_notebook: bool = True
    max_prompts_per_session: int = 5
    storage_dir: Optional[Path] = None
    keep_notebooks: bool = True
    timeout: int = EXECUTION_TIMEOUT_SEC
    strict_kernel_validation: bool = False
    strict_env_validation: bool = False
    sandbox_fallback_policy: SandboxFallbackPolicy = SandboxFallbackPolicy.WARN_AND_FALLBACK
    workspace_dir: Optional[Path] = None


class SandboxExecutionError(RuntimeError):
    """Raised when notebook execution fails and fallback is disabled."""
