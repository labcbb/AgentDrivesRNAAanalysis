"""Runtime environment detection for sRNAgent (aligned with omicverse)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


DEFAULT_EXPECTED_CONDA_ENV = os.environ.get("SRNAGENT_CONDA_ENV", "srnagent")


@dataclass
class RuntimeEnvironment:
    python_executable: str
    conda_env: Optional[str]
    conda_prefix: Optional[str]
    kernel_name: str
    expected_env: str
    env_matches_expected: bool

    def to_dict(self) -> dict:
        return {
            "python_executable": self.python_executable,
            "conda_env": self.conda_env or "",
            "conda_prefix": self.conda_prefix or "",
            "kernel_name": self.kernel_name,
            "expected_env": self.expected_env,
            "env_matches_expected": self.env_matches_expected,
        }


def detect_conda_environment() -> Optional[str]:
    """Detect current conda environment name (same strategy as omicverse)."""
    conda_env = os.environ.get("CONDA_DEFAULT_ENV")
    if conda_env:
        return conda_env

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        return Path(conda_prefix).name

    prefix = sys.prefix.lower()
    if "conda" in prefix or "anaconda" in prefix or "miniforge" in prefix or "mambaforge" in prefix:
        return Path(sys.prefix).name

    return None


def get_kernel_name(conda_env: Optional[str]) -> str:
    if conda_env:
        return f"conda-env-{conda_env}-py"
    return "python3"


def detect_runtime_environment(
    expected_env: Optional[str] = None,
) -> RuntimeEnvironment:
    expected = expected_env or DEFAULT_EXPECTED_CONDA_ENV
    conda_env = detect_conda_environment()
    conda_prefix = os.environ.get("CONDA_PREFIX")
    return RuntimeEnvironment(
        python_executable=sys.executable,
        conda_env=conda_env,
        conda_prefix=conda_prefix,
        kernel_name=get_kernel_name(conda_env),
        expected_env=expected,
        env_matches_expected=(conda_env == expected) if conda_env else False,
    )


def validate_expected_environment(
    runtime: RuntimeEnvironment,
    *,
    strict: bool = False,
) -> List[str]:
    """Return human-readable warnings; raise when strict and env mismatches."""
    warnings: List[str] = []

    if not runtime.conda_env:
        warnings.append(
            "当前未检测到 conda 环境。建议: conda activate "
            f"{runtime.expected_env}"
        )
    elif not runtime.env_matches_expected:
        warnings.append(
            f"当前 conda 环境是 '{runtime.conda_env}'，期望 '{runtime.expected_env}'。"
            f"请运行: conda activate {runtime.expected_env}"
        )

    if strict and warnings:
        raise RuntimeError("\n".join(warnings))

    return warnings
