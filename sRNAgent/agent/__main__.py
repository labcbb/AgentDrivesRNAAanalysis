"""CLI entry: python -m sRNAgent.agent \"download SRP464891 fastq\""""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .srn_agent import SRNAgent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the sRNAgent tool-loop agent.")
    parser.add_argument("query", nargs="?", help="User task in natural language")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print loaded skills/functions and exit (no LLM call)",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if args.status:
        from .bootstrap import initialize_registries

        function_registry, skill_registry, _ = initialize_registries(cwd=Path.cwd())
        print(json.dumps({
            "skills": list(skill_registry.skill_metadata.keys()),
            "functions": [
                e.get("full_name")
                for e in function_registry.find("fastq")
            ],
        }, indent=2, ensure_ascii=False))
        return 0

    if not args.query:
        parser.error("query is required unless --status is set")

    agent = SRNAgent(cwd=Path.cwd())
    answer = agent.run(args.query)
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
