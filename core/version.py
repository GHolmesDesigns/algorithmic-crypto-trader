"""The application version, read from the one place it is written: ``pyproject.toml``.

Every display (the FastAPI document, the startup banner, ``/health/detail`` and the
dashboard) calls ``application_version``. Nothing else in the code carries the number.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path

DISTRIBUTION_NAME = "algorithmic-crypto-trader"
UNKNOWN_VERSION = "unknown"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"


def read_pyproject_version(path: Path = PYPROJECT_PATH) -> str | None:
    """The ``[project] version`` in a ``pyproject.toml``, or ``None`` if it cannot be read."""

    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, ValueError):
        return None
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) and version else None


@lru_cache(maxsize=1)
def application_version() -> str:
    """The version of the code that is running; ``"unknown"`` when it cannot be determined.

    The ``pyproject.toml`` next to the code wins. A source checkout, every card worktree and
    the Docker image (which copies it beside the packages) all have one, and it is the file a
    pull request edits. Installed metadata is only the fallback: an editable install records
    the version once, and one install is shared by every worktree, so after a bump it lags
    until someone reinstalls.
    """

    from_file = read_pyproject_version(PYPROJECT_PATH)
    if from_file is not None:
        return from_file
    try:
        return metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION
