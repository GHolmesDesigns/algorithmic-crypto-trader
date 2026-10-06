"""Fail a pull request that changes shipped code without raising the application version.

The rule is written in CONTRIBUTING.md under "Versioning". This script enforces the part a
machine can judge: the version is ``MAJOR.MINOR.PATCH``, it is not lower than the base
branch's, and it is higher than the base branch's when the diff touches anything that ships.
Whether MINOR or PATCH was the right part to raise stays with the reviewer.

It reads two git revisions and nothing else: no network, no credentials, no working tree.
If either revision cannot be read it fails, because an unreadable base proves nothing.

    python tools/check_version_bump.py --base origin/main [--head HEAD]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence

# The directories and files the Dockerfile copies into the image, plus the Dockerfile itself.
# tests/test_version_bump_check.py fails if the Dockerfile copies something this list misses.
SHIPPED_DIRECTORIES = (
    "alembic/",
    "api/",
    "app/",
    "brokers/",
    "core/",
    "data/",
    "db/",
    "execution/",
    "portfolio/",
    "risk/",
    "strategy/",
)
SHIPPED_FILES = ("Dockerfile", "alembic.ini", "deploy/entrypoint.sh")

_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_RULE = "See CONTRIBUTING.md, section Versioning."

Version = tuple[int, int, int]


def parse_version(text: object) -> Version | None:
    """``MAJOR.MINOR.PATCH`` with no prefix, suffix or leading zeros; anything else is ``None``."""

    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def is_shipped(path: str) -> bool:
    return path in SHIPPED_FILES or path.startswith(SHIPPED_DIRECTORIES)


def _project(pyproject: str) -> dict[str, object]:
    try:
        project = tomllib.loads(pyproject).get("project", {})
    except tomllib.TOMLDecodeError:
        return {}
    return project if isinstance(project, dict) else {}


def project_version(pyproject: str) -> str | None:
    version = _project(pyproject).get("version")
    return version if isinstance(version, str) else None


def runtime_dependencies(pyproject: str) -> frozenset[str]:
    dependencies = _project(pyproject).get("dependencies")
    if not isinstance(dependencies, list):
        return frozenset()
    return frozenset(str(item) for item in dependencies)


def check(base_pyproject: str, head_pyproject: str, changed: Sequence[str]) -> list[str]:
    """The reasons the pull request fails the version rule; empty when it passes."""

    base_text = project_version(base_pyproject)
    base = parse_version(base_text)
    if base is None:
        return [
            f"The base branch's version {base_text!r} cannot be read as MAJOR.MINOR.PATCH, "
            "so the pull request cannot be compared with it."
        ]
    head_text = project_version(head_pyproject)
    head = parse_version(head_text)
    if head is None:
        return [
            f"The version {head_text!r} in pyproject.toml is not MAJOR.MINOR.PATCH "
            "(digits only, no leading zeros, no prefix or suffix)."
        ]
    if head < base:
        return [f"The version {head_text} is lower than the base branch's {base_text}."]
    if head > base:
        return []
    reasons = [f"{path} ships in the image" for path in sorted(changed) if is_shipped(path)]
    if runtime_dependencies(base_pyproject) != runtime_dependencies(head_pyproject):
        reasons.append("the runtime dependency list changed")
    if not reasons:
        return []
    shown = reasons[:5]
    if len(reasons) > len(shown):
        shown.append(f"and {len(reasons) - len(shown)} more")
    return [
        f"The version is still {head_text}, the same as the base branch, but "
        + "; ".join(shown)
        + ". Raise MINOR or PATCH in pyproject.toml."
    ]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git revision of the base branch")
    parser.add_argument("--head", default="HEAD", help="git revision of the change (default HEAD)")
    arguments = parser.parse_args(argv)
    try:
        base_pyproject = _git("show", f"{arguments.base}:pyproject.toml")
        head_pyproject = _git("show", f"{arguments.head}:pyproject.toml")
        changed = _git(
            "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "-z",
            arguments.base, arguments.head,
        ).split("\0")  # fmt: skip
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Version check failed: could not read git history ({error}). {_RULE}")
        return 1
    problems = check(base_pyproject, head_pyproject, [path for path in changed if path])
    if problems:
        for problem in problems:
            print(f"Version check failed: {problem}")
        print(_RULE)
        return 1
    print(f"Version check passed: {project_version(head_pyproject)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
