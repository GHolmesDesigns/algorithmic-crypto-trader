"""The preflight version check: shipped changes raise the version, everything else may not."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_version_bump.py"
_spec = importlib.util.spec_from_file_location("check_version_bump", SCRIPT)
assert _spec is not None and _spec.loader is not None
bump = importlib.util.module_from_spec(_spec)
sys.modules["check_version_bump"] = bump
_spec.loader.exec_module(bump)

DEPENDENCIES = ("fastapi>=0.115,<1", "httpx>=0.27,<1")


def pyproject(version: str | None, dependencies=DEPENDENCIES, dev=("pytest>=8.3,<9",)) -> str:
    lines = ["[project]", 'name = "algorithmic-crypto-trader"']
    if version is not None:
        lines.append(f'version = "{version}"')
    lines.append("dependencies = [" + ", ".join(f'"{item}"' for item in dependencies) + "]")
    lines.append("[project.optional-dependencies]")
    lines.append("dev = [" + ", ".join(f'"{item}"' for item in dev) + "]")
    return "\n".join(lines) + "\n"


SHIPPED = [
    "app/main.py",
    "api/dashboard.py",
    "alembic/versions/0012_new_table.py",
    "brokers/gemini.py",
    "core/version.py",
    "data/storage.py",
    "db/models.py",
    "execution/engine.py",
    "portfolio/ledger.py",
    "risk/kill_switch.py",
    "strategy/base.py",
    "Dockerfile",
    "alembic.ini",
    "deploy/entrypoint.sh",
]
NOT_SHIPPED = [
    "docs/user-manual.md",
    "tests/test_app.py",
    ".github/workflows/repo-preflight.yml",
    "docker-compose.yml",
    "deploy/docker-compose.vps.yml",
    "deploy/drill.sh",
    "tools/check_version_bump.py",
    "probes/common.py",
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "pyproject.toml",
    "application/notes.txt",
]


@pytest.mark.parametrize("path", SHIPPED)
def test_a_shipped_change_with_the_same_version_fails(path):
    problems = bump.check(pyproject("0.1.0"), pyproject("0.1.0"), [path])
    assert len(problems) == 1
    assert path in problems[0]
    assert "still 0.1.0" in problems[0]
    assert "Raise MINOR or PATCH" in problems[0]


@pytest.mark.parametrize("path", NOT_SHIPPED)
def test_a_change_that_does_not_ship_may_keep_the_version(path):
    assert bump.check(pyproject("0.1.0"), pyproject("0.1.0"), [path]) == []


def test_a_pull_request_with_no_changed_files_passes():
    assert bump.check(pyproject("0.1.0"), pyproject("0.1.0"), []) == []


@pytest.mark.parametrize(
    ("base", "head"),
    [
        ("0.1.0", "0.1.1"),
        ("0.1.0", "0.2.0"),
        ("0.1.5", "0.2.0"),
        ("0.1.0", "1.0.0"),
        ("0.9.0", "0.10.0"),
        ("0.9.9", "0.10.0"),
        ("1.9.9", "2.0.0"),
    ],
)
def test_raising_any_part_passes_with_or_without_shipped_changes(base, head):
    assert bump.check(pyproject(base), pyproject(head), SHIPPED) == []
    assert bump.check(pyproject(base), pyproject(head), NOT_SHIPPED) == []


@pytest.mark.parametrize(
    ("base", "head"),
    [("0.2.0", "0.1.9"), ("0.2.1", "0.2.0"), ("1.0.0", "0.9.9"), ("0.10.0", "0.9.0")],
)
def test_a_lowered_version_fails_even_with_nothing_shipped(base, head):
    problems = bump.check(pyproject(base), pyproject(head), NOT_SHIPPED)
    assert len(problems) == 1
    assert f"{head} is lower than the base branch's {base}" in problems[0]


@pytest.mark.parametrize(
    "head",
    [
        "1.0",
        "v1.0.0",
        "1.0.0rc1",
        "1.0.0-dev",
        "01.0.0",
        "1.0.0.0",
        "",
        "1.2.x",
        "1.2.3 ",
        " 1.2.3",
    ],
)
def test_a_malformed_version_fails(head):
    problems = bump.check(pyproject("0.1.0"), pyproject(head), NOT_SHIPPED)
    assert len(problems) == 1
    assert "not MAJOR.MINOR.PATCH" in problems[0]


def test_a_missing_or_non_text_head_version_fails():
    assert "not MAJOR.MINOR.PATCH" in bump.check(pyproject("0.1.0"), pyproject(None), [])[0]
    non_text = '[project]\nname = "x"\nversion = 3\n'
    assert "not MAJOR.MINOR.PATCH" in bump.check(pyproject("0.1.0"), non_text, [])[0]
    assert "not MAJOR.MINOR.PATCH" in bump.check(pyproject("0.1.0"), "not [[toml", [])[0]


@pytest.mark.parametrize(
    "base", [pyproject(None), "not [[toml", pyproject("0.1"), "", pyproject("v0.1.0")]
)
def test_an_unreadable_base_version_fails_closed(base):
    problems = bump.check(base, pyproject("0.2.0"), [])
    assert len(problems) == 1
    assert "base branch's version" in problems[0]
    assert "cannot be read" in problems[0]


def test_a_runtime_dependency_change_needs_a_bump():
    added = (*DEPENDENCIES, "psycopg[binary]>=3.2,<4")
    problems = bump.check(pyproject("0.1.0"), pyproject("0.1.0", dependencies=added), [])
    assert len(problems) == 1
    assert "runtime dependency list changed" in problems[0]
    assert bump.check(pyproject("0.1.0"), pyproject("0.1.1", dependencies=added), []) == []


def test_reordered_dependencies_and_dev_dependencies_are_not_a_shipped_change():
    reordered = pyproject("0.1.0", dependencies=tuple(reversed(DEPENDENCIES)))
    assert bump.check(pyproject("0.1.0"), reordered, []) == []
    dev_only = pyproject("0.1.0", dev=("pytest>=8.3,<9", "ruff>=0.6,<1"))
    assert bump.check(pyproject("0.1.0"), dev_only, []) == []


def test_a_long_list_of_shipped_files_is_summarized():
    paths = [f"api/page_{number}.py" for number in range(12)]
    problems = bump.check(pyproject("0.1.0"), pyproject("0.1.0"), paths)
    assert len(problems) == 1
    assert "and 7 more" in problems[0]
    assert problems[0].count("api/page_") == 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.0", (0, 1, 0)),
        ("10.20.30", (10, 20, 30)),
        ("0.0.0", (0, 0, 0)),
        ("1.0", None),
        ("1.0.0\n", None),
        ("١.٠.٠", None),
        (None, None),
        (100, None),
    ],
)
def test_parse_version_is_strict(text, expected):
    assert bump.parse_version(text) == expected


def dockerfile_sources() -> list[str]:
    sources = []
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"COPY\s+(\S+)\s+\S+\s*$", line)
        if match:
            sources.append(match.group(1))
    return sources


def test_everything_the_dockerfile_copies_counts_as_shipped():
    sources = dockerfile_sources()
    assert "app" in sources
    for source in sources:
        if source == "pyproject.toml":
            continue  # only its dependency list ships; the check reads that separately
        assert bump.is_shipped(f"{source}/x" if (ROOT / source).is_dir() else source), source
    assert bump.is_shipped("Dockerfile")


def test_the_dockerfile_still_copies_pyproject_for_the_installed_version():
    assert "pyproject.toml" in dockerfile_sources()


# The command line, against a real repository with a base commit and a change on top.


def git(repo: Path, *args: str) -> None:
    command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.test"]
    subprocess.run(
        [*command, "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text(pyproject("0.1.0"), encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('base')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("base\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def run(repo: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~1", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_cli_fails_a_shipped_change_without_a_bump_and_names_the_file_and_rule(repo):
    (repo / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    result = run(repo)
    assert result.returncode == 1
    assert "app/main.py ships in the image" in result.stdout
    assert "Raise MINOR or PATCH" in result.stdout
    assert "See CONTRIBUTING.md, section Versioning." in result.stdout


def test_cli_passes_a_shipped_change_with_a_bump(repo):
    (repo / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(pyproject("0.2.0"), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    result = run(repo)
    assert result.returncode == 0
    assert "Version check passed: 0.2.0." in result.stdout


def test_cli_passes_a_docs_only_change(repo):
    (repo / "docs" / "notes.md").write_text("changed\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    assert run(repo).returncode == 0


def test_cli_sees_a_file_moved_out_of_a_shipped_directory(repo):
    (repo / "scripts").mkdir()
    git(repo, "mv", "app/main.py", "scripts/main.py")
    git(repo, "commit", "-q", "-m", "move")
    result = run(repo)
    assert result.returncode == 1
    assert "app/main.py ships in the image" in result.stdout


def test_cli_fails_closed_when_the_base_cannot_be_read(repo):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "origin/does-not-exist"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "could not read git history" in result.stdout


# The written rule, the workflow and the script must agree.


def test_the_contributing_rule_names_every_shipped_path_and_each_kind_of_bump():
    rule = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    versioning = rule[rule.index("## Versioning") :]
    for directory in bump.SHIPPED_DIRECTORIES:
        assert f"`{directory.rstrip('/')}/`" in versioning, directory
    for name in (*bump.SHIPPED_FILES, "alembic.ini"):
        assert f"`{name}`" in versioning, name
    for kind in (
        "**MINOR**",
        "**PATCH**",
        "**No bump**",
        "`no bump`",
        "tools/check_version_bump.py",
    ):
        assert kind in versioning, kind


def test_the_agent_handoff_list_asks_for_the_version_change():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    handoff = guide[guide.index("## Pull-request handoff") :]
    assert "the application version change, old to new, or `no bump` and why" in handoff


def test_the_preflight_workflow_runs_the_check_on_pull_requests_against_the_base_branch():
    workflow = (ROOT / ".github" / "workflows" / "repo-preflight.yml").read_text(encoding="utf-8")
    step = workflow[workflow.index("Check the application version") :]
    step = step[: step.index("Install project and development dependencies")]
    assert "if: github.event_name == 'pull_request'" in step
    assert 'python tools/check_version_bump.py --base "origin/${BASE_REF}"' in step
    assert "BASE_REF: ${{ github.base_ref }}" in step
    assert "secrets." not in step
