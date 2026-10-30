"""Guard that skill examples only use tool APIs that actually exist.

Skills are the agent's prompt-level API contract. A stale example (renamed
parameter, dropped argument, wrong function) makes the agent emit code that
raises TypeError at runtime, so every ``sa.<cat>.<tool>(...)`` call in a
skill is resolved against the real namespace here.

Resolution mirrors how the agent runs code: ``import sRNAgent as sa`` then
attribute access.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import sRNAgent as sa

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills"
CALL_RE = re.compile(r"\bsa\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _skill_files() -> list[Path]:
    return sorted(SKILL_DIR.rglob("*.md"))


def _resolve(ref: str):
    obj = sa
    try:
        for part in ref.split(".")[1:]:
            obj = getattr(obj, part)
    except AttributeError:
        return None
    return obj


def _call_kwargs(text: str, match: re.Match) -> list[str]:
    start = match.end()
    depth = 1
    index = start
    while index < len(text) and depth:
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
        index += 1
    body = text[start:index - 1]
    return re.findall(r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", body)


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda p: str(p.relative_to(SKILL_DIR)))
def test_skill_example_tools_and_params_exist(skill_file: Path):
    text = skill_file.read_text(errors="replace")
    problems: list[str] = []
    for match in CALL_RE.finditer(text):
        ref = f"sa.{match.group(1)}.{match.group(2)}"
        func = _resolve(ref)
        if func is None:
            problems.append(f"{ref} does not exist")
            continue
        try:
            params = set(inspect.signature(func).parameters)
        except (TypeError, ValueError):
            continue
        for keyword in _call_kwargs(text, match):
            if keyword not in params and keyword != "kwargs":
                problems.append(f"{ref} has no parameter {keyword!r}")
    assert not problems, f"{skill_file}: " + "; ".join(problems)
