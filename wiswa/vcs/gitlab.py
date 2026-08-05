"""
GitLab REST API helpers used during VCS sync.

Wraps `gidgetlab <https://gidgetlab.readthedocs.io>`_ with an adapter,
:py:class:`NiquestsGitLabAPI`, that uses :py:class:`niquests.AsyncSession` for transport — the
same shape as the upstream :py:class:`gidgetlab.aiohttp.GitLabAPI` adapter.
"""
from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlparse
import asyncio
import getpass
import logging
import os
import re

from gidgetlab import abc as gl_abc
from gidgetlab.exceptions import BadRequest, HTTPException
from typing_extensions import override
import keyring
import keyring.errors

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from wiswa.typing import PackageManager, ProjectType
    import niquests

    from .typing import (
        Badge,
        BranchProtectionOverrides,
        ProjectApprovals,
        ProjectSettings,
        PushRules,
        RemoteSettings,
    )

__all__ = ('GITLAB_TOKEN_ENV', 'MAINTAINER_ACCESS_LEVEL', 'MIRROR_PROJECT_SETTINGS_OVERRIDES',
           'USER_AGENT', 'NiquestsGitLabAPI', 'apply_project_settings', 'base_url',
           'configure_project', 'desired_gitlab_badges', 'encode_project_path',
           'fetch_project_default_branch', 'get_gitlab_token', 'gitlab_merged_remote_tables',
           'parse_badges', 'patch_protected_branch', 'project_path', 'protect_branches',
           'protect_tags', 'repository_uri_hostname', 'sync_badges', 'trigger_housekeeping')

GITLAB_TOKEN_ENV = 'GITLAB_TOKEN'  # noqa: S105
"""
Environment variable consulted first when resolving a GitLab personal access token.

:meta hide-value:
"""

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
_RECOVERABLE_SETTINGS_STATUSES = frozenset(
    {HTTPStatus.BAD_REQUEST, HTTPStatus.UNPROCESSABLE_ENTITY})


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


def _rejected_setting_keys(error: HTTPException, settings: Mapping[str, Any]) -> frozenset[str]:
    detail = error.args[0] if error.args else ''
    if isinstance(detail, dict):
        return frozenset(key for key in detail if key in settings)
    rejected: set[str] = set()
    for message in str(detail).lower().split(', '):
        if matches := [k for k in settings if message.startswith(f"{k.replace('_', ' ')} ")]:
            rejected.add(max(matches, key=len))
    return frozenset(rejected)


async def _put_project_settings(api: gl_abc.GitLabAPI, encoded_project_path: str,
                                project_settings: ProjectSettings) -> None:
    remaining: dict[str, Any] = dict(project_settings)
    while True:
        try:
            await api.put(f'/projects/{encoded_project_path}', data=dict(remaining))
        except BadRequest as e:  # noqa: PERF203  # each retry must observe its own rejection.
            if e.status_code not in _RECOVERABLE_SETTINGS_STATUSES:
                raise
            if not (rejected := _rejected_setting_keys(e, remaining)):
                log.warning('Could not apply GitLab project settings: %s.', e)
                return
            for key in rejected:
                del remaining[key]
            log.warning('GitLab rejected project setting(s) %s: %s. Retrying without them.',
                        ', '.join(f'`{key}`' for key in sorted(rejected)), e)
            if not remaining:
                log.warning('No GitLab project settings left to apply.')
                return
        else:
            log.info('Applied project settings.')
            return


async def apply_project_settings(api: gl_abc.GitLabAPI,
                                 encoded_project_path: str,
                                 *,
                                 project_settings: ProjectSettings,
                                 push_rules: PushRules | None = None,
                                 project_approvals: ProjectApprovals | None = None) -> None:
    """
    Apply opinionated project settings, push rules, and approvals to a GitLab project.

    Settings that GitLab refuses with a ``400`` or ``422`` (for example a ``pages_access_level``
    that conflicts with the project visibility) are removed from the request body, and the
    remaining settings are retried, so one invalid field does not discard the whole update. A
    refusal that names no recognisable setting is logged and the settings update is abandoned.
    Any other settings failure, notably an unusable token, propagates to the caller. Push rule
    and approval failures are always logged and skipped.

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
    await _put_project_settings(api, encoded_project_path, project_settings)
    if push_rules:
        try:
            try:
                await api.put(f'/projects/{encoded_project_path}/push_rule', data=dict(push_rules))
            except HTTPException:
                await api.post(f'/projects/{encoded_project_path}/push_rule', data=dict(push_rules))
        except HTTPException as e:
            log.warning('Could not apply GitLab push rules: %s.', e)
        else:
            log.info('Applied push rules.')
    if project_approvals:
        try:
            await api.post(f'/projects/{encoded_project_path}/approvals',
                           data=dict(project_approvals))
        except HTTPException as e:
            log.warning('Could not apply GitLab project approvals: %s.', e)
        else:
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
    extras: dict[str, object] = {**overrides} if overrides else {}
    for name in sorted(set(names) - existing):
        body: dict[str, object] = {
            'merge_access_level': MAINTAINER_ACCESS_LEVEL,
            'name': name,
            'push_access_level': MAINTAINER_ACCESS_LEVEL,
        } | extras
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


def repository_uri_hostname(uri: str) -> str:
    """
    Return the hostname portion of a GitLab repository URI.

    Parameters
    ----------
    uri : str
        A repository URI such as ``https://gitlab.example.com/group/project.git``.

    Returns
    -------
    str
        The bare hostname, or an empty string when *uri* has no host component.
    """
    return urlparse(uri).hostname or ''


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


async def fetch_project_default_branch(api: gl_abc.GitLabAPI,
                                       encoded_project_path: str) -> str | None:
    """
    Return the GitLab project's default branch name.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.

    Returns
    -------
    str | None
        Default branch name reported by ``GET /projects/:id``, or :py:data:`None` when the
        project payload does not include one.
    """
    project = await api.getitem(f'/projects/{encoded_project_path}')
    if isinstance(project, dict) and isinstance((branch := project.get('default_branch')), str):
        return branch
    return None


async def patch_protected_branch(api: gl_abc.GitLabAPI, encoded_project_path: str, branch_name: str,
                                 body: BranchProtectionOverrides) -> None:
    """
    Patch an existing protected branch on a GitLab project.

    Parameters
    ----------
    api : gidgetlab.abc.GitLabAPI
        An authenticated gidgetlab client.
    encoded_project_path : str
        Project identifier returned by :py:func:`encode_project_path`.
    branch_name : str
        Name of the protected branch to update; URL-encoded internally.
    body : BranchProtectionOverrides
        PATCH body applied to the protected branch (for example
        ``{'allow_force_push': 'true'}``).
    """
    from urllib.parse import quote  # noqa: PLC0415

    encoded_branch = quote(branch_name, safe='')
    await api.patch(f'/projects/{encoded_project_path}/protected_branches/{encoded_branch}',
                    data=dict(body))
    log.info('Patched protected branch `%s`.', branch_name)


def gitlab_merged_remote_tables(
    gitlab: RemoteSettings | None,
) -> tuple[ProjectSettings, PushRules, ProjectApprovals, BranchProtectionOverrides]:
    """
    Return the four GitLab API request bodies from a merged remote-settings mapping.

    Defaults and per-field overrides are expected to have been merged at the source (Jsonnet
    layering for Wiswa projects).

    Parameters
    ----------
    gitlab : RemoteSettings | None
        Mapping containing ``project_settings``, ``push_rules``, ``project_approvals``, and
        ``default_branch_protection`` subtables. :py:data:`None` is treated as an empty mapping.

    Returns
    -------
    tuple[ProjectSettings, PushRules, ProjectApprovals, BranchProtectionOverrides]
        ``(project_settings, push_rules, project_approvals,
        default_branch_protection)`` ready to feed the GitLab REST API.
    """
    glb = gitlab or {}
    return (glb.get('project_settings') or cast('ProjectSettings', {}), glb.get('push_rules')
            or cast('PushRules', {}), glb.get('project_approvals') or cast('ProjectApprovals', {}),
            glb.get('default_branch_protection') or cast('BranchProtectionOverrides', {}))


def desired_gitlab_badges(*,
                          repository_uri: str,
                          want_tests: bool = False,
                          project_type: ProjectType | None = None,
                          using_django: bool = False,
                          package_manager: PackageManager | None = None,
                          stubs_only: bool = False) -> list[Badge]:
    """
    Return the GitLab badge list Wiswa applies to a project.

    Parameters
    ----------
    repository_uri : str
        HTTPS URI of the GitLab project; only the host is consulted to build absolute badge
        URLs.
    want_tests : bool
        Whether the project has a test suite; toggles the Coverage and pytest badges.
    project_type : ProjectType | None
        Project type identifier; controls the Python-specific badge set when ``'python'``.
    using_django : bool
        Whether the project uses Django; adds the Django badge when ``project_type`` is
        ``'python'``.
    package_manager : PackageManager | None
        Python package manager identifier; adds either the uv or Poetry badge when
        ``project_type`` is ``'python'``.
    stubs_only : bool
        Whether the project consists of typing stubs only; suppresses the pytest badge.

    Returns
    -------
    list[Badge]
        Ordered badge definitions suitable for the GitLab project badges API.
    """
    base = base_url(repository_uri)
    project = f'{base}/%{{project_path}}'
    branch = '%{default_branch}'
    pipelines_link = f'{project}/-/pipelines'
    badges: list[Badge] = [{
        'image_url': f'{project}/badges/{branch}/pipeline.svg?ignore_skipped=true',
        'link_url': pipelines_link,
        'name': 'QA',
    }]
    if want_tests:
        badges.append({
            'image_url': f'{project}/badges/{branch}/coverage.svg?ignore_skipped=true',
            'link_url': pipelines_link,
            'name': 'Coverage',
        })
    badges.append({
        'image_url': f'{project}/-/badges/release.svg',
        'link_url': f'{project}/-/releases',
        'name': 'Latest Release',
    })
    if project_type == 'python':
        if using_django:
            badges.append({
                'image_url': 'https://img.shields.io/badge/django-092E20?logo=django',
                'link_url': 'https://djangoproject.com',
                'name': 'Django',
            })
        badges.append({
            'image_url': 'https://www.mypy-lang.org/static/mypy_badge.svg',
            'link_url': 'https://mypy-lang.org/',
            'name': 'mypy',
        })
        if package_manager == 'uv':
            badges.append({
                'image_url': 'https://img.shields.io/badge/uv-261230?logo=astral',
                'link_url': 'https://docs.astral.sh/uv/',
                'name': 'uv',
            })
        else:
            badges.append({
                'image_url': 'https://img.shields.io/badge/Poetry-242d3e?logo=poetry',
                'link_url': 'https://python-poetry.org',
                'name': 'Poetry',
            })
        if want_tests and not stubs_only:
            badges.append({
                'image_url': ('https://img.shields.io/badge/pytest-zz'
                              '?logo=Pytest&labelColor=black&color=black'),
                'link_url': 'https://docs.pytest.org/en/stable/',
                'name': 'pytest',
            })
        badges.append({
            'image_url': ('https://img.shields.io/endpoint?url=https://raw.githubusercontent.com'
                          '/astral-sh/ruff/main/assets/badge/v2.json'),
            'link_url': 'https://github.com/astral-sh/ruff',
            'name': 'Ruff',
        })
    badges.extend([{
        'image_url': 'https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit',
        'link_url': 'https://github.com/pre-commit/pre-commit',
        'name': 'pre-commit'
    }, {
        'image_url': 'https://img.shields.io/badge/Prettier-black?logo=prettier',
        'link_url': 'https://prettier.io/',
        'name': 'Prettier'
    }])
    return badges


async def _configure(session: niquests.AsyncSession,
                     *,
                     token: str,
                     repository_uri: str,
                     description: str = '',
                     homepage: str = '',
                     keywords: Iterable[str] = (),
                     default_branch: str | None = None,
                     gitlab_config: RemoteSettings | None = None,
                     want_tests: bool = False,
                     project_type: ProjectType | None = None,
                     using_django: bool = False,
                     package_manager: PackageManager | None = None,
                     stubs_only: bool = False) -> None:
    encoded = encode_project_path(project_path(repository_uri))
    if not encoded:
        log.warning('Could not derive GitLab project path from `%s`.', repository_uri)
        return
    api = NiquestsGitLabAPI(session, USER_AGENT, access_token=token, url=base_url(repository_uri))
    project_settings, push_rules, project_approvals, default_branch_protection = (
        gitlab_merged_remote_tables(gitlab_config))
    project_settings['description'] = description
    project_settings['topics'] = [x.replace(' ', '-') for x in keywords]
    project_settings['homepage_url'] = homepage
    await apply_project_settings(api,
                                 encoded,
                                 project_approvals=project_approvals or None,
                                 project_settings=project_settings,
                                 push_rules=push_rules or None)
    if default_branch_protection:
        resolved_default = (await fetch_project_default_branch(api, encoded) or default_branch)
        if resolved_default:
            await patch_protected_branch(api, encoded, resolved_default, default_branch_protection)
        else:
            log.warning('Could not determine default branch for `%s`.', repository_uri)
    await sync_badges(
        api, encoded,
        desired_gitlab_badges(repository_uri=repository_uri,
                              want_tests=want_tests,
                              project_type=project_type,
                              using_django=using_django,
                              package_manager=package_manager,
                              stubs_only=stubs_only))


async def configure_project(session: niquests.AsyncSession,
                            *,
                            repository_uri: str,
                            description: str = '',
                            homepage: str = '',
                            keywords: Iterable[str] = (),
                            default_branch: str | None = None,
                            gitlab_config: RemoteSettings | None = None,
                            want_tests: bool = False,
                            project_type: ProjectType | None = None,
                            using_django: bool = False,
                            package_manager: PackageManager | None = None,
                            stubs_only: bool = False) -> None:
    """
    Configure a GitLab project (settings, description, topics, badges, protected branch).

    Authentication uses the :py:data:`GITLAB_TOKEN_ENV` environment variable first, then the
    system keyring (see :py:func:`get_gitlab_token`). The caller
    is responsible for deciding whether to invoke this function (for example by checking a
    ``using_gitlab`` flag); this routine always attempts to run when called.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session used by the gidgetlab adapter.
    repository_uri : str
        HTTPS URI of the GitLab project (used to derive both the API host and the project path).
    description : str
        Short project description applied to ``project_settings.description``.
    homepage : str
        External homepage URL applied to ``project_settings.homepage_url``.
    keywords : Iterable[str]
        Free-form keywords; each entry has spaces replaced with hyphens before being stored as a
        GitLab topic.
    default_branch : str | None
        Fallback default branch used when the project does not yet have one and a branch is
        required for protection rules.
    gitlab_config : RemoteSettings | None
        Opinionated GitLab tables (``project_settings``, ``push_rules``, ``project_approvals``,
        ``default_branch_protection``). Empty dictionaries and :py:data:`None` are treated as
        absent.
    want_tests : bool
        Whether the project has a test suite; controls the Coverage and pytest badges.
    project_type : ProjectType | None
        Project type used to shape the badge set (Python-specific badges are added when this is
        ``'python'``).
    using_django : bool
        Whether the project uses Django; adds the Django badge when ``project_type`` is
        ``'python'``.
    package_manager : PackageManager | None
        Python package manager identifier; adds either the uv or Poetry badge when
        ``project_type`` is ``'python'``.
    stubs_only : bool
        Whether the project consists of typing stubs only; suppresses the pytest badge.
    """
    host = repository_uri_hostname(repository_uri)
    token = get_gitlab_token(host)
    if not token:
        log.warning('No GitLab token (set %s or keyring `wiswa-gitlab:%s`).', GITLAB_TOKEN_ENV,
                    host)
        return
    try:
        await _configure(session,
                         token=token,
                         repository_uri=repository_uri,
                         description=description,
                         homepage=homepage,
                         keywords=keywords,
                         default_branch=default_branch,
                         gitlab_config=gitlab_config,
                         want_tests=want_tests,
                         project_type=project_type,
                         using_django=using_django,
                         package_manager=package_manager,
                         stubs_only=stubs_only)
    except HTTPException as e:
        log.warning('Caught error updating GitLab project: %s.', e)
        log.debug('%r', e)
