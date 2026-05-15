"""
GitHub REST API helpers used during VCS sync.

Wraps `gidgethub <https://gidgethub.readthedocs.io>`_ with an adapter,
:py:class:`NiquestsGitHubAPI`, that uses :py:class:`niquests.AsyncSession` for transport — the
same shape as the upstream :py:class:`gidgethub.aiohttp.GitHubAPI` and
:py:class:`gidgethub.httpx.GitHubAPI` adapters.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
import asyncio
import logging

from gidgethub import HTTPException, abc as gh_abc
from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Mapping

    import niquests

    from .typing import GitHubRepository

__all__ = (
    'USER_AGENT',
    'NiquestsGitHubAPI',
    'fetch_repository',
    'protected_branch_names',
    'protected_tag_patterns',
    'slug_from_uri',
)

log = logging.getLogger(__name__)

USER_AGENT = 'wiswa-vcs'
"""
Requester string passed to :py:class:`gidgethub.abc.GitHubAPI` on construction.

:meta hide-value:
"""


class NiquestsGitHubAPI(gh_abc.GitHubAPI):
    """
    :py:class:`gidgethub.abc.GitHubAPI` implementation backed by :py:mod:`niquests`.

    Mirrors :py:class:`gidgethub.aiohttp.GitHubAPI`: pass an open
    :py:class:`niquests.AsyncSession` plus the usual gidgethub constructor arguments and use the
    instance like any other gidgethub client.
    """
    def __init__(self, session: niquests.AsyncSession, requester: str, **kwargs: Any) -> None:
        """
        Initialise the adapter.

        Parameters
        ----------
        session : niquests.AsyncSession
            Open async HTTP session. Lifetime is the caller's responsibility.
        requester : str
            Identifier used as the value of the GitHub ``User-Agent`` header.
        kwargs : Any
            Forwarded to :py:class:`gidgethub.abc.GitHubAPI` (``oauth_token``, ``base_url``,
            ``cache``).
        """
        self._session = session
        super().__init__(requester, **kwargs)

    @override
    async def _request(self,
                       method: str,
                       url: str,
                       headers: Mapping[str, str],
                       body: bytes = b'') -> tuple[int, dict[str, str], bytes]:
        response = await self._session.request(method=method,
                                               url=url,
                                               headers=dict(headers),
                                               data=body or None)
        status_code = response.status_code
        content = response.content
        if status_code is None or content is None:
            msg = 'niquests returned an incomplete HTTP response.'
            raise RuntimeError(msg)
        headers_mapping = cast('Mapping[str, str | bytes]', response.headers)
        response_headers: dict[str, str] = {
            key: value if isinstance(value, str) else value.decode()
            for key, value in headers_mapping.items()
        }
        return status_code, response_headers, content

    @override
    async def sleep(self, seconds: float) -> None:
        """
        Suspend the current task for *seconds*.

        Parameters
        ----------
        seconds : float
            Sleep duration. Used by gidgethub when waiting out a rate-limit response.
        """
        await asyncio.sleep(seconds)


def slug_from_uri(uri: str) -> str:
    """
    Return the ``owner/repo`` slug from a GitHub repository URI.

    Parameters
    ----------
    uri : str
        A repository URI such as ``https://github.com/owner/repo.git`` or an already-bare
        ``owner/repo`` slug.

    Returns
    -------
    str
        The repository slug with any leading slash and trailing ``.git`` stripped.
    """
    if '://' not in uri:
        return uri.strip('/').removesuffix('.git')
    return urlparse(uri).path.strip('/').removesuffix('.git')


async def fetch_repository(api: gh_abc.GitHubAPI, slug: str) -> GitHubRepository:
    """
    Return the GitHub repository metadata for *slug*.

    Parameters
    ----------
    api : gidgethub.abc.GitHubAPI
        An authenticated gidgethub client.
    slug : str
        Repository slug in ``owner/repo`` form.

    Returns
    -------
    GitHubRepository
        Decoded JSON body from ``GET /repos/{slug}``.
    """
    return cast('GitHubRepository', dict(await api.getitem(f'/repos/{slug}')))


async def protected_branch_names(api: gh_abc.GitHubAPI, slug: str) -> set[str]:
    """
    List the names of all protected branches on *slug*.

    Parameters
    ----------
    api : gidgethub.abc.GitHubAPI
        An authenticated gidgethub client.
    slug : str
        Repository slug in ``owner/repo`` form.

    Returns
    -------
    set[str]
        Names of protected branches; empty if the call fails.
    """
    names: set[str] = set()
    try:
        async for branch in api.getiter(f'/repos/{slug}/branches', {'protected': 'true'}):
            if branch.get('protected'):
                names.add(branch['name'])
    except HTTPException as e:
        log.warning('Could not list GitHub protected branches: %s.', e)
    return names


async def protected_tag_patterns(api: gh_abc.GitHubAPI, slug: str) -> set[str]:
    """
    List the tag-targeting ruleset include patterns on *slug*.

    Parameters
    ----------
    api : gidgethub.abc.GitHubAPI
        An authenticated gidgethub client.
    slug : str
        Repository slug in ``owner/repo`` form.

    Returns
    -------
    set[str]
        Tag patterns with the ``refs/tags/`` prefix removed; empty if the rulesets endpoint
        cannot be read.
    """
    patterns: set[str] = set()
    try:
        rulesets = [ruleset async for ruleset in api.getiter(f'/repos/{slug}/rulesets')]
    except HTTPException as e:
        log.warning('Could not list GitHub rulesets: %s.', e)
        return patterns
    for ruleset in rulesets:
        if ruleset.get('target') != 'tag':
            continue
        try:
            detail = await api.getitem(f"/repos/{slug}/rulesets/{ruleset['id']}")
        except HTTPException:
            continue
        ref_name = (detail.get('conditions') or {}).get('ref_name') or {}
        for ref in ref_name.get('include') or []:
            pattern = ref.replace('refs/tags/', '')
            if pattern and pattern != '~ALL':
                patterns.add(pattern)
    return patterns
