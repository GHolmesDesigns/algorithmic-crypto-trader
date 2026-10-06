"""The application version has one source, pyproject.toml, and every display reads it."""

import importlib.metadata
import tomllib
from pathlib import Path

import httpx
import pytest
from app.main import create_app
from core import version as version_module
from core.guards import CredentialScope, StartupSettings, startup_banner
from core.models import TradingMode
from core.version import (
    UNKNOWN_VERSION,
    application_version,
    read_pyproject_version,
)

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = {"x-operator-token": "operator-secret"}
ADMIN = {"x-operator-token": "admin-secret"}
SETTINGS = StartupSettings(
    TradingMode.PAPER, CredentialScope.VIEW, "", "postgresql://unused", "INFO"
)
SHOWN = "9.8.7"


@pytest.fixture(autouse=True)
def fresh_version_cache():
    application_version.cache_clear()
    yield
    application_version.cache_clear()


def write_pyproject(directory: Path, text: str | None = None) -> Path:
    path = directory / "pyproject.toml"
    path.write_text(text or f'[project]\nname = "x"\nversion = "{SHOWN}"\n', encoding="utf-8")
    return path


def point_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(version_module, "PYPROJECT_PATH", path)
    application_version.cache_clear()


def application(monkeypatch, **options):
    monkeypatch.setenv("OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OPERATOR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.delenv("KILL_SWITCH_FILE", raising=False)
    return create_app(SETTINGS, **options)


async def fetch(app, path, headers=None, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {}, **kwargs)


def test_the_runtime_version_is_the_one_written_in_the_repository_pyproject():
    written = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert application_version() == written["project"]["version"]


def test_the_repository_version_is_major_minor_patch():
    # The preflight check enforces the same format on every pull request.
    assert len(application_version().split(".")) == 3
    assert all(part.isdecimal() for part in application_version().split("."))


@pytest.mark.parametrize(
    "text",
    [
        None,
        "this is [[not toml",
        "[tool.ruff]\nline-length = 100\n",
        '[project]\nname = "x"\n',
        '[project]\nname = "x"\nversion = ""\n',
        '[project]\nname = "x"\nversion = 3\n',
        'project = "not a table"\n',
    ],
)
def test_an_unreadable_pyproject_gives_no_version(tmp_path, text):
    path = write_pyproject(tmp_path, text) if text else tmp_path / "missing.toml"
    assert read_pyproject_version(path) is None


def test_the_pyproject_beside_the_code_beats_stale_installed_metadata(monkeypatch, tmp_path):
    # One editable install is shared by every worktree, so its recorded version lags a bump.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.1")
    point_at(monkeypatch, write_pyproject(tmp_path))
    assert application_version() == SHOWN


def test_installed_metadata_is_the_fallback_when_there_is_no_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "4.5.6")
    point_at(monkeypatch, tmp_path / "missing.toml")
    assert application_version() == "4.5.6"


def test_a_version_that_cannot_be_found_is_unknown_never_invented(monkeypatch, tmp_path):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    point_at(monkeypatch, tmp_path / "missing.toml")
    assert application_version() == UNKNOWN_VERSION


async def test_changing_only_the_pyproject_version_changes_every_display(monkeypatch, tmp_path):
    point_at(monkeypatch, write_pyproject(tmp_path))
    app = application(monkeypatch)
    assert app.version == SHOWN
    assert startup_banner(SETTINGS).endswith(f" | version={SHOWN}")
    assert "live_confirmation=not applicable" in startup_banner(SETTINGS)
    for headers in (OPERATOR, ADMIN):
        detail = await fetch(app, "/health/detail", headers)
        assert detail.json()["application"]["version"] == SHOWN
        state = await fetch(app, "/operator/state", headers)
        assert state.json()["application"]["version"] == SHOWN
        page = await fetch(app, "/operator", headers)
        assert page.status_code == 200
        assert f"<div><dt>Application version</dt><dd>{SHOWN}</dd></div>" in page.text
        assert "<dt>Strategy version</dt><dd>unknown</dd>" in page.text
        fragment = await fetch(app, "/operator/fragment", headers)
        assert f"<dd>{SHOWN}</dd>" in fragment.text


async def test_the_dashboard_says_the_version_is_unknown_when_it_cannot_be_found(
    monkeypatch, tmp_path
):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    point_at(monkeypatch, tmp_path / "missing.toml")
    page = await fetch(application(monkeypatch), "/operator", OPERATOR)
    assert f"<dt>Application version</dt><dd>{UNKNOWN_VERSION}</dd>" in page.text


async def test_signed_out_pages_and_health_never_show_the_version(monkeypatch, tmp_path):
    point_at(monkeypatch, write_pyproject(tmp_path))
    app = application(monkeypatch)
    health = await fetch(app, "/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    for path, headers in (
        ("/operator/login", {"accept": "text/html"}),
        ("/operator", {"accept": "text/html"}),
        ("/operator/state", {}),
        ("/health/detail", {}),
    ):
        response = await fetch(app, path, headers)
        assert SHOWN not in response.text, path
    icon = await fetch(app, "/favicon.ico")
    assert icon.status_code == 200
    assert SHOWN.encode() not in icon.content
    assert "version" not in {name.lower() for name in icon.headers}


class ForbiddenBroker:
    async def get_balances(self):
        raise AssertionError("reading the version must not call the provider")

    async def get_positions(self):
        raise AssertionError("reading the version must not call the provider")


async def test_reading_the_version_makes_no_provider_call(monkeypatch, tmp_path):
    point_at(monkeypatch, write_pyproject(tmp_path))
    app = application(monkeypatch, broker=ForbiddenBroker())
    detail = await fetch(app, "/health/detail", OPERATOR)
    assert detail.status_code == 200
    assert detail.json()["application"]["version"] == SHOWN
