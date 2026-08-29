"""Skill discovery and loading for sRNAgent (filesystem-based, progressive disclosure)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore

    _YAML_AVAILABLE = True
except Exception:
    yaml = None
    _YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


def _package_root() -> Path:
    return Path(__file__).resolve().parent


@dataclass
class SkillMetadata:
    name: str
    slug: str
    description: str
    path: Path
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
        }


@dataclass
class SkillDefinition:
    name: str
    slug: str
    description: str
    path: Path
    body: str
    metadata: Dict[str, str] = field(default_factory=dict)

    def prompt_instructions(self, max_chars: int = 4000) -> str:
        text = (self.body or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rstrip() + "..."


class SkillRegistry:
    """Load skills from ``{skill_root}/*/SKILL.md`` with progressive disclosure."""

    def __init__(self, skill_root: Path):
        self.skill_root = skill_root
        self._skill_metadata: Dict[str, SkillMetadata] = {}
        self._full_skills_cache: Dict[str, SkillDefinition] = {}

    @property
    def skill_metadata(self) -> Dict[str, SkillMetadata]:
        return self._skill_metadata

    @property
    def skills(self) -> Dict[str, SkillDefinition]:
        for slug in self._skill_metadata:
            if slug not in self._full_skills_cache:
                self.load_full_skill(slug)
        return self._full_skills_cache

    def load(self) -> None:
        if not self.skill_root.exists():
            return

        discovered: Dict[str, SkillMetadata] = {}
        for skill_file in sorted(self.skill_root.glob("*/SKILL.md")):
            metadata = self._parse_skill_metadata(skill_file)
            if not metadata:
                continue
            key = metadata.slug.lower()
            if key in discovered:
                logger.warning("Duplicate skill slug '%s'; keeping first.", key)
                continue
            discovered[key] = metadata
            logger.info("Loaded skill metadata '%s' from %s", metadata.name, skill_file)
        self._skill_metadata = discovered

    def load_full_skill(self, slug: str) -> Optional[SkillDefinition]:
        slug_lower = slug.lower()
        if slug_lower in self._full_skills_cache:
            return self._full_skills_cache[slug_lower]

        metadata = self._skill_metadata.get(slug_lower)
        if not metadata:
            return None

        definition = self._parse_skill_file(metadata.path / "SKILL.md")
        if definition:
            self._full_skills_cache[slug_lower] = definition
        return definition

    def _parse_skill_metadata(self, skill_file: Path) -> Optional[SkillMetadata]:
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Unable to read skill file %s: %s", skill_file, exc)
            return None

        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return None

        try:
            closing_index = lines.index("---", 1)
        except ValueError:
            return None

        metadata = self._parse_frontmatter(lines[1:closing_index])
        raw_name = metadata.get("name")
        description = metadata.get("description")
        title = metadata.get("title") or metadata.get("display_title") or raw_name
        slug_value = metadata.get("slug")
        if not slug_value:
            slug_value = (
                raw_name
                if self._looks_like_slug(raw_name)
                else self._slugify(title)
            )
        if not (title and description and slug_value):
            return None

        return SkillMetadata(
            name=str(title),
            slug=str(slug_value),
            description=str(description),
            path=skill_file.parent,
            metadata=metadata,
        )

    def _parse_skill_file(self, skill_file: Path) -> Optional[SkillDefinition]:
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Unable to read skill file %s: %s", skill_file, exc)
            return None

        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return None

        try:
            closing_index = lines.index("---", 1)
        except ValueError:
            return None

        metadata = self._parse_frontmatter(lines[1:closing_index])
        raw_name = metadata.get("name")
        description = metadata.get("description")
        title = metadata.get("title") or metadata.get("display_title") or raw_name
        slug_value = metadata.get("slug")
        if not slug_value:
            slug_value = (
                raw_name
                if self._looks_like_slug(raw_name)
                else self._slugify(title)
            )
        if not (title and description and slug_value):
            return None

        body = "\n".join(lines[closing_index + 1 :]).strip()
        return SkillDefinition(
            name=str(title),
            slug=str(slug_value),
            description=str(description),
            path=skill_file.parent,
            body=body,
            metadata=metadata,
        )

    @staticmethod
    def _parse_frontmatter(lines: Iterable[str]) -> Dict[str, str]:
        if _YAML_AVAILABLE:
            text = "\n".join(list(lines))
            try:
                loaded = yaml.safe_load(text)  # type: ignore[attr-defined]
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                return {str(k): str(v) if isinstance(v, str) else str(v) for k, v in loaded.items()}

        metadata: Dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"')
        return metadata

    @staticmethod
    def _looks_like_slug(value: Optional[str]) -> bool:
        if not value or not isinstance(value, str):
            return False
        return re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is not None

    @staticmethod
    def _slugify(value: Optional[str], max_len: int = 64) -> str:
        if not value:
            return ""
        slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
        slug = re.sub(r"-+", "-", slug)
        if len(slug) > max_len:
            slug = slug[:max_len].strip("-")
        return slug


def discover_skill_roots(package_root: Optional[Path] = None, cwd: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """Return skill roots in ascending precedence (later overrides earlier)."""
    root = package_root or _package_root()
    workdir = cwd or Path.cwd()

    roots: List[Tuple[str, Path]] = []

    def _append(label: str, path: Path) -> None:
        resolved = path.resolve()
        if any(existing == resolved for _, existing in roots):
            return
        roots.append((label, resolved))

    _append("Bundled", root / "skills")
    _append("Workspace", workdir / "skills")
    _append("Workspace", workdir / ".claude" / "skills")
    return roots


def build_skill_registry(
    package_root: Optional[Path] = None,
    cwd: Optional[Path] = None,
) -> SkillRegistry:
    """Merge skills from bundled + workspace paths (workspace wins on duplicate slug)."""
    root = package_root or _package_root()
    workdir = cwd or Path.cwd()
    roots = discover_skill_roots(root, workdir)

    merged_metadata: Dict[str, SkillMetadata] = {}
    merged_skills: Dict[str, SkillDefinition] = {}

    for label, skill_root in roots:
        if not skill_root.exists():
            continue
        registry = SkillRegistry(skill_root)
        registry.load()
        if not registry.skill_metadata:
            continue
        for slug, metadata in registry.skill_metadata.items():
            if slug in merged_metadata:
                logger.info("%s skill '%s' overrides bundled definition", label, metadata.name)
            merged_metadata[slug] = metadata
            full = registry.load_full_skill(slug)
            if full:
                merged_skills[slug] = full
        logger.info("Loaded %d skills from %s (%s)", len(registry.skill_metadata), skill_root, label)

    default_root = roots[0][1] if roots else (root / "skills")
    combined = SkillRegistry(default_root)
    combined._skill_metadata = merged_metadata
    combined._full_skills_cache = merged_skills
    return combined


def format_skill_overview(registry: SkillRegistry) -> str:
    if not registry.skill_metadata:
        return ""
    lines = [
        f"- **{skill.name}** (`{skill.slug}`) — {skill.description}"
        for skill in sorted(registry.skill_metadata.values(), key=lambda s: s.name.lower())
    ]
    return "\n".join(lines)


__all__ = [
    "SkillMetadata",
    "SkillDefinition",
    "SkillRegistry",
    "build_skill_registry",
    "discover_skill_roots",
    "format_skill_overview",
]
