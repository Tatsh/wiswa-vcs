"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn
import os

from click.testing import CliRunner
import pytest

if os.getenv('_PYTEST_RAISE', '0') != '0':  # pragma no cover

    @pytest.hookimpl(tryfirst=True)
    def pytest_exception_interact(call: pytest.CallInfo[None]) -> NoReturn:
        assert call.excinfo is not None
        raise call.excinfo.value

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(excinfo: pytest.ExceptionInfo[BaseException]) -> NoReturn:
        raise excinfo.value


@pytest.fixture(autouse=True)
def recover_stale_process_cwd(request: pytest.FixtureRequest) -> None:
    """
    Recover when the process cwd was removed mid-session.

    Some Gentoo build phases retain temporary directories aggressively; the process working
    directory can then point at a path that no longer exists, breaking ``Path.cwd()`` calls
    before ``monkeypatch.chdir`` saves the prior cwd.
    """
    try:
        Path.cwd()
    except FileNotFoundError:
        os.chdir(Path(request.config.rootpath))


@pytest.fixture
def runner() -> CliRunner:
    """Return a Click :py:class:`~click.testing.CliRunner` instance."""
    return CliRunner()
