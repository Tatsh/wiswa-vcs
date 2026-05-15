"""
GitLab REST API helpers used during VCS sync.

Wraps `gidgetlab <https://gidgetlab.readthedocs.io>`_ with an adapter,
:py:class:`NiquestsGitLabAPI`, that uses :py:class:`niquests.AsyncSession` for transport — the
same shape as the upstream :py:class:`gidgetlab.aiohttp.GitLabAPI` adapter.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlparse
import asyncio
import logging
import re

from gidgetlab import abc as gl_abc
from gidgetlab.exceptions import HTTPException
from typing_extensions import override

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    import niquests

    from .typing import (
        Badge,
        BranchProtectionOverrides,
        ProjectApprovals,
        ProjectSettings,
        PushRules,
    )

__all__ = (
    'MAINTAINER_ACCESS_LEVEL',
    'MIRROR_PROJECT_SETTINGS_OVERRIDES',
    'USER_AGENT',
    'NiquestsGitLabAPI',
    'apply_project_settings',
    'base_url',
    'encode_project_path',
    'parse_badges',
    'project_path',
    'protect_branches',
    'protect_tags',
    'sync_badges',
    'trigger_housekeeping',
)

log = logging.getLogger(__name__)

MAINTAINER_ACCESS_LEVEL = 40
"""GitLab access level integer for the *Maintainer* role.

:meta hide-value:
"""
MIRROR_PROJECT_SETTINGS_OVERRIDES: ProjectSettings = {
    'builds_access_level': 'disabled',
    'lfs_enabled': 'false',
    'merge_requests_access_level': 'disabled',
    'service_desk_enabled': 'false',
}
"""Project setting overrides applied to read-only mirrors.

Disables CI/CD, Git LFS, merge requests, and service desk on the GitLab side so the mirror
does not invite contributions and does not run pipelines that would conflict with the source
repository.

:meta hide-value:
"""
USER_AGENT = f'wiswa-vcs/{__version__}'
"""Requester string passed to :py:class:`gidgetlab.abc.GitLabAPI` on construction.

Carries the installed wiswa-vcs version as the product token so GitLab request logs can
attribute traffic to a specific release.

:meta hide-value:
"""

_BADGE_IMAGE_RE = re.compile(r'^\s*\.\.\s+image::\s+(\S+)\s*$')
_BADGE_OPTION_RE = re.compile(r'^\s+:(\S+?):\s*(.*)$')
_BADGE_OPTION_PREFIX_RE = re.compile(r'^\s+:')


class NiquestsGitLabAPI(gl_abc.GitLabAPI):
    """
    :py:class:`gidgetlab.abc.GitLabAPI` implementation backed by :py:mod:`niquests`.

    Mirrors :py:class:`gidgetlab.aiohttp.GitLabAPI`: pass an open
    :py:class:`niquests.AsyncSession` plus the usual gidgetlab constructor arguments and use the
    instance like any other gidgetlab client.
    """
    def __init__(self, session: niquests.AsyncSession, requester: str, **kwargs: Any) -> None:
        """
        Initialise the adapter.

        Parameters
        ----------
        session : niquests.AsyncSession
            Open async HTTP session. Lifetime is the caller's responsibility.
        requester : str
            Identifier used as the value of the GitLab ``User-Agent`` header.
        kwargs : Any
            Forwarded to :py:class:`gidgetlab.abc.GitLabAPI` (``access_token``, ``url``,
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
            Sleep duration. Used by gidgetlab when waiting out a rate-limit response.
        """
        await asyncio.sleep(seconds)


def base_url(uri: str) -> str:
    """
    Return the ``scheme://host`` portion of a GitLab repository URI.

    Parameters
    ----------
    uri : str
        A repository URI such as ``https://gitlab.com/group/project.git``.

    Returns
    -------
    str
        The base URL with no trailing slash.
    """
    parsed = urlparse(uri)
    return f'{parsed.scheme}://{parsed.netloc}'


def project_path(uri: str) -> str:
    """
    Return the ``group/project`` portion of a GitLab repository URI.

    Parameters
    ----------
    uri : str
        A repository URI such as ``https://gitlab.com/group/project.git``.

    Returns
    -------
    str
        The project path with any leading slash and trailing ``.git`` stripped.
    """
    return urlparse(uri).path.strip('/').removesuffix('.git')


def encode_project_path(path: str) -> str:
    """
    Return *path* URL-encoded for use as a GitLab project identifier.

    Parameters
    ----------
    path : str
        Project path in ``group/subgroup/project`` form.

    Returns
    -------
    str
        The same path with every reserved character percent-encoded (notably ``/`` → ``%2F``).
    """
    return quote(path, safe='')


def parse_badges(text: str) -> Iterator[Badge]:
    """
    Yield badges parsed from a ``docs/badges.rst``-style document.

    Recognises ``.. image:: URL`` directives with ``:target:`` and ``:alt:`` options. Entries
    missing either option, or whose image URL is not an absolute HTTP URL, are skipped.

    Parameters
    ----------
    text : str
        Full file contents.

    Yields
    ------
    Badge
        Badge definitions in source order.
    """
    lines = text.splitlines()
    cursor = 0
    while cursor < len(lines):
        image_match = _BADGE_IMAGE_RE.match(lines[cursor])
        if not image_match:
            cursor += 1
            continue
        image_url = image_match.group(1)
        link_url = ''
        alt = ''
        cursor += 1
        while cursor < len(lines) and _BADGE_OPTION_PREFIX_RE.match(lines[cursor]):
            option_match = _BADGE_OPTION_RE.match(lines[cursor])
            if option_match:
                key, value = option_match.group(1), option_match.group(2).strip()
                if key == 'target':
                    link_url = value
                elif key == 'alt':
                    alt = value
            cursor += 1
        if alt and link_url and image_url.startswith('http'):
            yield {'image_url': image_url, 'link_url': link_url, 'name': alt}


async def apply_project_settings(api: gl_abc.GitLabAPI,
                                 encoded_project_path: str,
                                 *,
                                 project_settings: ProjectSettings,
                                 push_rules: PushRules | None = None,
                                 project_approvals: ProjectApprovals | None = None) -> None:
    """
    Apply opinionated project settings, push rules, and approvals to a GitLab project.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    project_settings : ProjectSettings
        ``PUT /projects/:id`` request body.
    push_rules : PushRules | None
        Push rule body; created when missing, updated otherwise. Skipped when empty.
    project_approvals : ProjectApprovals | None
        Merge-request approval rule body. Skipped when empty.
    """
    await api.put(f'/projects/{encoded_project_path}', data=dict(project_settings))
    log.info('Applied project settings.')
    if push_rules:
        try:
            await api.put(f'/projects/{encoded_project_path}/push_rule', data=dict(push_rules))
        except HTTPException:
            await api.post(f'/projects/{encoded_project_path}/push_rule', data=dict(push_rules))
        log.info('Applied push rules.')
    if project_approvals:
        await api.post(f'/projects/{encoded_project_path}/approvals', data=dict(project_approvals))
        log.info('Applied project approvals.')


async def protect_branches(api: gl_abc.GitLabAPI,
                           encoded_project_path: str,
                           names: Iterable[str],
                           *,
                           overrides: BranchProtectionOverrides | None = None) -> None:
    """
    Ensure each branch *name* is protected on the project.

    Existing protected branches are left untouched. New entries are created with maintainer-level
    push and merge access, optionally overridden by *overrides*.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    names : Iterable[str]
        Branch names that must be protected.
    overrides : BranchProtectionOverrides | None
        Extra fields merged into the create body (for example
        ``{'allow_force_push': 'true'}``).
    """
    existing: set[str] = set()
    async for branch in api.getiter(f'/projects/{encoded_project_path}/protected_branches'):
        if name := branch.get('name'):
            existing.add(name)
    extras: dict[str, object] = dict(overrides) if overrides else {}
    for name in sorted(set(names) - existing):
        body: dict[str, object] = {
            'merge_access_level': MAINTAINER_ACCESS_LEVEL,
            'name': name,
            'push_access_level': MAINTAINER_ACCESS_LEVEL,
        }
        body.update(extras)
        await api.post(f'/projects/{encoded_project_path}/protected_branches', data=body)
        log.info('Protected branch `%s`.', name)


async def protect_tags(api: gl_abc.GitLabAPI, encoded_project_path: str,
                       patterns: Iterable[str]) -> None:
    """
    Ensure each tag *pattern* is protected on the project.

    Existing protected tag patterns are left untouched.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    patterns : Iterable[str]
        Tag name patterns to protect; wildcards such as ``v*`` are accepted.
    """
    existing: set[str] = set()
    async for tag in api.getiter(f'/projects/{encoded_project_path}/protected_tags'):
        if name := tag.get('name'):
            existing.add(name)
    for name in sorted(set(patterns) - existing):
        await api.post(f'/projects/{encoded_project_path}/protected_tags',
                       data={
                           'create_access_level': MAINTAINER_ACCESS_LEVEL,
                           'name': name
                       })
        log.info('Protected tag `%s`.', name)


async def sync_badges(api: gl_abc.GitLabAPI, encoded_project_path: str,
                      desired: Iterable[Badge]) -> None:
    """
    Synchronise project-level badges to match *desired*.

    Existing badges with matching names are updated in place when their URLs differ; missing
    badges are created. Badges not in *desired* are left untouched.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    desired : Iterable[Badge]
        Badge definitions; the ``name`` field is the stable identifier.
    """
    by_name: dict[str, dict[str, Any]] = {}
    async for badge in api.getiter(f'/projects/{encoded_project_path}/badges'):
        if badge.get('kind') == 'project' and (name := badge.get('name')):
            by_name[name] = badge
    for badge_def in desired:
        name = badge_def['name']
        if name in by_name:
            current = by_name[name]
            if (current.get('image_url') == badge_def['image_url']
                    and current.get('link_url') == badge_def['link_url']):
                continue
            await api.put(f"/projects/{encoded_project_path}/badges/{current['id']}",
                          data=dict(badge_def))
            log.info('Updated badge `%s`.', name)
        else:
            await api.post(f'/projects/{encoded_project_path}/badges', data=dict(badge_def))
            log.info('Created badge `%s`.', name)


async def trigger_housekeeping(api: gl_abc.GitLabAPI, encoded_project_path: str) -> None:
    """
    Trigger GitLab housekeeping for the given project.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    """
    await api.post(f'/projects/{encoded_project_path}/housekeeping', data=None)
    log.info('Triggered housekeeping.')
