"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn
import os

from click.testing import CliRunner
import pytest

from wiswa.vcs.github import GITHUB_TOKEN_ENV, clear_tag_cache

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


@pytest.fixture(autouse=True)
def reset_github_tag_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Drop the in-process and on-disk GitHub tag cache before each test.

    Redirects ``XDG_CACHE_HOME`` and ``XDG_CONFIG_HOME`` to ``tmp_path`` so tests cannot
    leak cache state between each other. Removes :py:data:`~wiswa.vcs.github.GITHUB_TOKEN_ENV`
    and stubs the keyring to return no token, then clears the in-process cache.
    """
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg-cache'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    # GitHub API lookups resolve a token; never read the developer's real token from the
    # environment or keyring.
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    monkeypatch.setattr('wiswa.vcs.github.keyring.get_password', lambda *_: None)
    clear_tag_cache()


@pytest.fixture
def runner() -> CliRunner:
    """Return a Click :py:class:`~click.testing.CliRunner` instance."""
    return CliRunner()
