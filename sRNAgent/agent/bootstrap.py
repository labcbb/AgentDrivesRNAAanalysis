"""Initialize function + skill registries and execution backend for sRNAgent."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from .._registry import get_registry
from ..skill_registry import SkillRegistry, build_skill_registry, format_skill_overview
from .agent_config import ExecutionConfig
from .execution import ExecutionBackend, initialize_execution_backend


def initialize_registries(
    cwd: Optional[Path] = None,
) -> Tuple[object, SkillRegistry, str]:
    """Load function registry and skill registry."""
    function_registry = get_registry()
    function_registry._ensure_hydrated()

    skill_registry = build_skill_registry(cwd=cwd)
    overview = format_skill_overview(skill_registry)
    return function_registry, skill_registry, overview


def initialize_agent_runtime(
    project_root: Path,
    cwd: Optional[Path] = None,
    execution_config: Optional[ExecutionConfig] = None,
) -> Tuple[object, SkillRegistry, str, ExecutionBackend]:
    """Load registries + notebook/in-process execution backend."""
    function_registry, skill_registry, overview = initialize_registries(cwd=cwd)
    execution = initialize_execution_backend(project_root, config=execution_config)
    return function_registry, skill_registry, overview, execution
