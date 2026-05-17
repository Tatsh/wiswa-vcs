"""Authentication token lookup for remote VCS hosts."""
from __future__ import annotations

import getpass
import logging
import os

import keyring
import keyring.errors

__all__ = ('GITLAB_TOKEN_ENV', 'get_gitlab_token')

log = logging.getLogger(__name__)

GITLAB_TOKEN_ENV = 'GITLAB_TOKEN'  # noqa: S105
"""Environment variable consulted first when resolving a GitLab personal access token.

:meta hide-value:
"""


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
