"""sRNAgent — Agent-driven small RNA-seq analysis."""

from __future__ import annotations

from . import agent
from .Tools import alignment, diff, fastq, quant, reference
from ._registry import (
    export_registry,
    find_function,
    get_function_help,
    get_registry,
    import_registry,
    list_functions,
    recommend_function,
    register_function,
)
from .agent import SRNAgent, initialize_registries
from .skill_registry import SkillRegistry, build_skill_registry

__version__ = "0.1.0"

__all__ = [
    "agent",
    "alignment",
    "diff",
    "fastq",
    "quant",
    "reference",
    "SRNAgent",
    "SkillRegistry",
    "build_skill_registry",
    "initialize_registries",
    "register_function",
    "find_function",
    "get_registry",
    "list_functions",
    "get_function_help",
    "recommend_function",
    "export_registry",
    "import_registry",
    "__version__",
]
