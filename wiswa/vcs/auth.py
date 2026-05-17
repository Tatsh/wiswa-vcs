"""Authentication token lookup for remote VCS hosts."""
from __future__ import annotations

import getpass
import logging
import os

import keyring
import keyring.errors

__all__ = ('GITHUB_TOKEN_ENV', 'GITLAB_TOKEN_ENV', 'get_github_token', 'get_gitlab_token')

log = logging.getLogger(__name__)

GITHUB_TOKEN_ENV = 'GITHUB_TOKEN'  # noqa: S105
"""Environment variable consulted first when resolving a GitHub personal access token.

:meta hide-value:
"""
GITLAB_TOKEN_ENV = 'GITLAB_TOKEN'  # noqa: S105
"""Environment variable consulted first when resolving a GitLab personal access token.

:meta hide-value:
"""


def get_github_token(host: str) -> str | None:
    """
    Resolve a GitHub personal access token from the environment or host-scoped keyring.

    Looks first at the :py:data:`GITHUB_TOKEN_ENV` environment variable. Falls back to the
    system keyring, trying the service name ``wiswa-github:<host>`` with the OS username
    first, and the legacy ``tmu-github-api`` service second (so credentials stored by older
    Wiswa installations continue to work).

    Parameters
    ----------
    host : str
        Hostname of the GitHub instance, for example ``github.com`` or
        ``github.example.com``. An empty string disables keyring lookup.

    Returns
    -------
    str | None
        The resolved token, or :py:data:`None` when no token is available and the keyring
        backend is missing or empty.
    """
    if token := os.environ.get(GITHUB_TOKEN_ENV):
        return token
    if not host:
        return None
    user = getpass.getuser()
    try:
        token = keyring.get_password(f'wiswa-github:{host}', user)
        if token:
            return token
        return keyring.get_password('tmu-github-api', user)
    except keyring.errors.NoKeyringError:
        log.warning('No keyring backend available.')
        return None


def get_gitlab_token(host: str) -> str | None:
    """
    Resolve a GitLab personal access token from the environment or host-scoped keyring.

    Looks first at the :py:data:`GITLAB_TOKEN_ENV` environment variable. Falls back to the
    system keyring, trying the service name ``wiswa-gitlab:<host>`` with the OS username
    first, and the bare hostname second (for older Wiswa installations that stored the token
    under the host as the username).

    Parameters
    ----------
    host : str
        Hostname of the GitLab instance, for example ``gitlab.com`` or
        ``gitlab.example.com``. An empty string disables keyring lookup.

    Returns
    -------
    str | None
        The resolved token, or :py:data:`None` when no token is available and the keyring
        backend is missing or empty.
    """
    if token := os.environ.get(GITLAB_TOKEN_ENV):
        return token
    if not host:
        return None
    user = getpass.getuser()
    try:
        token = keyring.get_password(f'wiswa-gitlab:{host}', user)
        if token:
            return token
        return keyring.get_password(f'wiswa-gitlab:{host}', host)
    except keyring.errors.NoKeyringError:
        log.warning('No keyring backend available.')
        return None
