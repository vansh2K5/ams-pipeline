"""Ingestion: detect each service's language and route it to the matching Phase 1 parser.

Adding a language is a parser change only: register a function that returns a ServiceSchema.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ams.parsers import java, python
from ams.schema import ServiceSchema

PARSERS: dict[str, Callable[[Path, str | None], ServiceSchema]] = {
    "java": java.parse,
    "python": python.parse,
}


def detect_language(root: Path) -> str:
    if (root / "pom.xml").exists() or (root / "build.gradle").exists() or any(root.rglob("*.java")):
        return "java"
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or any(root.rglob("*.py")):
        return "python"
    raise ValueError(f"no supported language found in {root} (supported: {', '.join(PARSERS)})")


def parse_service(root: Path, name: str | None = None) -> ServiceSchema:
    return PARSERS[detect_language(root)](root, name)
