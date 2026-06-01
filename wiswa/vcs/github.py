"""
GitHub REST API helpers used during VCS sync.

Wraps `gidgethub <https://gidgethub.readthedocs.io>`_ with an adapter,
:py:class:`NiquestsGitHubAPI`, that uses :py:class:`niquests.AsyncSession` for transport — the
same shape as the upstream :py:class:`gidgethub.aiohttp.GitHubAPI` and
:py:class:`gidgethub.httpx.GitHubAPI` adapters.

Release-tag and commit-SHA lookups (:py:func:`latest_release_tag`,
:py:func:`ref_commit_sha`) are cached in process for the lifetime of the run and persisted
to ``platformdirs.user_cache_path('wiswa') / 'github_tag_cache.json'`` so the cache is
shared with Wiswa itself. When the GitHub API responds with ``403`` or ``429``
(rate-limited), the disk cache is consulted as a last-resort fallback. The cache
deliberately lives under ``wiswa`` (not ``wiswa-vcs``) so a user with Wiswa already
installed pays no cold-cache cost the first time they invoke :py:mod:`wiswa.vcs`.

This module deliberately does **not** know about any specific repository. Callers that
need stricter tag rules for a particular owner/repo (for example ``google/yapf``, whose
tags must always start with ``v``) pass ``require_v_prefix=True`` to
:py:func:`latest_release_tag`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast
from urllib.parse import urlparse
import asyncio
import getpass
import json
import logging
import os
import re

from gidgethub import HTTPException, abc as gh_abc
from typing_extensions import override
import keyring
import keyring.errors
import platformdirs

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from packaging.version import Version
    import niquests

    from .typing import Repository, RepositoryConfig, Ruleset

__all__ = ('GITHUB_API_HEADERS', 'GITHUB_TOKEN_ENV', 'USER_AGENT', 'NiquestsGitHubAPI',
           'PagesBuildType', 'clear_tag_cache', 'configure_project', 'fetch_repository',
           'get_github_token', 'get_pages_build_type', 'latest_release_tag',
           'protected_branch_names', 'protected_tag_patterns', 'ref_commit_sha', 'slug_from_uri')

GITHUB_TOKEN_ENV = 'GITHUB_TOKEN'  # noqa: S105
"""
Environment variable consulted first when resolving a GitHub personal access token.

:meta hide-value:
"""

PagesBuildType: TypeAlias = Literal['legacy', 'workflow']
"""
Possible values for a GitHub Pages site's ``build_type`` field.

:meta hide-value:
"""

log = logging.getLogger(__name__)

USER_AGENT = f'wiswa-vcs/{__version__}'
"""
Requester string passed to :py:class:`gidgethub.abc.GitHubAPI` on construction.

Carries the installed wiswa-vcs version as the product token so GitHub request logs can
attribute traffic to a specific release.

:meta hide-value:
"""
GITHUB_API_HEADERS: dict[str, str] = {
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}
"""Default request headers for direct ``niquests`` calls to the GitHub REST API.

Use these when bypassing :py:class:`NiquestsGitHubAPI` (for example to request a non-default
``Accept`` media type such as ``application/vnd.github.sha``).

:meta hide-value:
"""
_GITHUB_TAG_DISK_FILENAME = 'github_tag_cache.json'
_GITHUB_RELEASES_PAGE_CAP = 20
_GITHUB_RELEASES_PER_PAGE = 100

_tag_cache: dict[str, str] = {}
_disk_store_memo_box: list[dict[str, str] | None] = [None]


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


async def fetch_repository(api: gh_abc.GitHubAPI, slug: str) -> Repository:
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
    Repository
        Decoded JSON body from ``GET /repos/{slug}``.
    """
    return cast('Repository', dict(await api.getitem(f'/repos/{slug}')))


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


async def get_pages_build_type(api: gh_abc.GitHubAPI, slug: str) -> PagesBuildType | None:
    """
    Return the GitHub Pages ``build_type`` for *slug*.

    Parameters
    ----------
    api : gidgethub.abc.GitHubAPI
        An authenticated gidgethub client.
    slug : str
        Repository slug in ``owner/repo`` form.

    Returns
    -------
    PagesBuildType | None
        ``'legacy'`` when Pages deploys from a branch, ``'workflow'`` when it uses GitHub
        Actions, or :py:data:`None` when the API call fails or the field is missing.
    """
    try:
        pages = await api.getitem(f'/repos/{slug}/pages')
    except HTTPException as e:
        log.debug('GitHub Pages API failed for `%s`: %s.', slug, e)
        return None
    build_type = pages.get('build_type') if isinstance(pages, dict) else None
    if build_type in {'legacy', 'workflow'}:
        return cast('PagesBuildType', build_type)
    return None


def _disk_cache_path() -> Path:
    return platformdirs.user_cache_path('wiswa', appauthor=False) / _GITHUB_TAG_DISK_FILENAME


def _read_disk_store() -> dict[str, str]:
    cached = _disk_store_memo_box[0]
    if cached is not None:
        return cached
    path = _disk_cache_path()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, TypeError):
        store: dict[str, str] = {}
    else:
        store = ({
            str(k): str(v)
            for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw, dict) else {})
    _disk_store_memo_box[0] = store
    return store


def _write_disk_entry(key: str, value: str) -> None:
    path = _disk_cache_path()
    store = _read_disk_store()
    store[key] = value
    text = f'{json.dumps(store, indent=2, sort_keys=True)}\n'
    tmp = path.with_suffix(f'{path.suffix}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(path)
    except OSError as exc:
        _disk_store_memo_box[0] = None
        log.debug('Could not persist GitHub tag cache: %s.', exc)


def clear_tag_cache() -> None:
    """
    Drop the in-process tag/SHA cache and the in-memory snapshot of the disk store.

    Does not delete the on-disk cache file under
    ``platformdirs.user_cache_path('wiswa')``; that file is only consulted when GitHub
    responds with ``403`` or ``429``. Intended for tests and long-lived processes that
    need a fresh view of GitHub.
    """
    _tag_cache.clear()
    _disk_store_memo_box[0] = None


def _blocked_status(response: object) -> int | None:
    code = getattr(response, 'status_code', None)
    if isinstance(code, int) and code in {403, 429}:
        return code
    return None


def _version_from_tag(tag: str) -> object | None:
    from packaging.version import InvalidVersion, parse as parse_version  # noqa: PLC0415

    try:
        return parse_version(tag.removeprefix('v'))
    except InvalidVersion:
        return None


def _tag_allowed_for_policy(tag: str, *, allow_suffixes: bool, require_v_prefix: bool) -> bool:
    if require_v_prefix and not tag.startswith('v'):
        return False
    if not allow_suffixes:
        return tag.startswith('v') and bool(re.search(r'\d$', tag))
    return True


async def _newest_release_tag_before_cutoff(
        session: niquests.AsyncSession, owner: str, repo: str, *, cutoff: datetime,
        allow_suffixes: bool, require_v_prefix: bool) -> tuple[str | None, int | None]:
    best: tuple[Version, str] | None = None
    for page in range(1, _GITHUB_RELEASES_PAGE_CAP + 1):
        r = await session.get(
            f'https://api.github.com/repos/{owner}/{repo}/releases'
            f'?per_page={_GITHUB_RELEASES_PER_PAGE}&page={page}',
            timeout=15)
        if (status := _blocked_status(r)) is not None:
            return None, status
        if not r.ok:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for rel in batch:
            if not isinstance(rel, dict) or rel.get('draft') or rel.get('prerelease'):
                continue
            tag = rel.get('tag_name')
            published = rel.get('published_at')
            if not isinstance(tag, str) or not tag or not isinstance(published, str):
                continue
            try:
                pub_dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
            except ValueError:
                continue
            if pub_dt > cutoff or not _tag_allowed_for_policy(
                    tag, allow_suffixes=allow_suffixes, require_v_prefix=require_v_prefix):
                continue
            ver = _version_from_tag(tag)
            if (ver is None or getattr(ver, 'is_prerelease', False)
                    or getattr(ver, 'is_devrelease', False)):
                continue
            ver_typed = cast('Version', ver)
            if best is None or ver_typed > best[0]:
                best = (ver_typed, tag)
        if len(batch) < _GITHUB_RELEASES_PER_PAGE:
            break
    return (best[1] if best else None), None


async def latest_release_tag(session: niquests.AsyncSession,
                             owner: str,
                             repo: str,
                             *,
                             skip_releases: bool = False,
                             allow_suffixes: bool = True,
                             require_v_prefix: bool = False,
                             min_release_age_minutes: int | None = None) -> str:
    """
    Return the latest published release tag for ``owner/repo`` on GitHub.

    Consults ``GET /repos/{owner}/{repo}/releases/latest`` first, then paginates
    ``GET /repos/{owner}/{repo}/tags``. Results are cached in process and persisted to the
    shared Wiswa disk cache so subsequent invocations within the rate-limit window are
    free. When GitHub returns ``403`` or ``429``, the disk cache is consulted as a
    last-resort fallback.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session.
    owner : str
        Repository owner login.
    repo : str
        Repository name.
    skip_releases : bool
        When :py:data:`True`, do not consult ``/releases/latest`` and use the tag list
        directly. Useful for repositories that publish tags but not GitHub releases.
    allow_suffixes : bool
        When :py:data:`False`, only consider tags that start with ``v`` and end in a digit
        (filters out things like ``v1.0-beta``).
    require_v_prefix : bool
        When :py:data:`True`, only consider tags that start with ``v`` regardless of
        *allow_suffixes*. Use this for repositories whose tag scheme allows non-``v``
        names that should not be picked (for example ``google/yapf``).
    min_release_age_minutes : int | None
        When set, only releases published at least this many minutes ago are considered
        (mirrors the npm minimum-release-age gate). When :py:data:`None`, no age gate is
        applied. The caller computes the value; this module does not read any user config.

    Returns
    -------
    str
        The selected release tag name.

    Raises
    ------
    ValueError
        If no tag can be determined and the disk cache has no entry to fall back on.
    """
    key = f'gh_{owner}/{repo}_{skip_releases}_{allow_suffixes}'
    if require_v_prefix:
        key += '_vp'
    if min_release_age_minutes is not None:
        key += f'_min_age{min_release_age_minutes}'
    if key in _tag_cache:
        return _tag_cache[key]
    version: str | None = None
    blocked_status: int | None = None
    if min_release_age_minutes is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=min_release_age_minutes)
        gated, status = await _newest_release_tag_before_cutoff(session,
                                                                owner,
                                                                repo,
                                                                cutoff=cutoff,
                                                                allow_suffixes=allow_suffixes,
                                                                require_v_prefix=require_v_prefix)
        if status is not None:
            blocked_status = status
        if gated:
            version = gated
        else:
            log.debug(
                'No GitHub release for `%s/%s` predates the %d-minute age gate; falling back.',
                owner, repo, min_release_age_minutes)
    if not version and not skip_releases:
        r = await session.get(f'https://api.github.com/repos/{owner}/{repo}/releases/latest',
                              timeout=15)
        if (status := _blocked_status(r)) is not None:
            blocked_status = status
        if r.ok:
            version = r.json().get('tag_name')
    if not version:
        r = await session.get(f'https://api.github.com/repos/{owner}/{repo}/tags', timeout=15)
        if (status := _blocked_status(r)) is not None:
            blocked_status = status
        if r.ok:
            tags = [x['name'] for x in r.json() if 'name' in x]
            if tags:
                if not allow_suffixes or require_v_prefix:
                    version = next((t for t in tags if t.startswith('v') and (
                        re.search(r'\d$', t) if not allow_suffixes else True)), None)
                else:
                    version = tags[0]
    if not version:
        if blocked_status is not None and (cached := _read_disk_store().get(key)):
            log.warning(
                'Using disk-cached GitHub tag `%s` for `%s/%s` after HTTP %d '
                '(likely rate-limited).', cached, owner, repo, blocked_status)
            _tag_cache[key] = cached
            return cached
        msg = f'Could not get latest tag for `{owner}/{repo}`.'
        raise ValueError(msg)
    _tag_cache[key] = version
    _write_disk_entry(key, version)
    return version


async def ref_commit_sha(session: niquests.AsyncSession, owner: str, repo: str, ref: str) -> str:
    """
    Resolve a Git ref to its underlying commit SHA via GitHub.

    Uses ``GET /repos/{owner}/{repo}/commits/{ref}`` with
    ``Accept: application/vnd.github.sha`` so GitHub returns just the 40-character SHA as
    plain text. Annotated tags are followed to the commit they point at; lightweight tags
    and branches resolve directly. The result is cached in process, persisted to the shared
    Wiswa disk cache, and falls back to that cache on ``403`` or ``429``.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session.
    owner : str
        Repository owner login.
    repo : str
        Repository name.
    ref : str
        Branch name, tag name, or full commit SHA to resolve.

    Returns
    -------
    str
        The 40-character hexadecimal commit SHA that *ref* resolves to.

    Raises
    ------
    ValueError
        If the SHA cannot be retrieved and no cached value is available.
    """
    key = f'gh_sha_{owner}/{repo}@{ref}'
    if key in _tag_cache:
        return _tag_cache[key]
    r = await session.get(f'https://api.github.com/repos/{owner}/{repo}/commits/{ref}',
                          headers={'Accept': 'application/vnd.github.sha'},
                          timeout=15)
    blocked = _blocked_status(r)
    if r.ok and (sha := (r.text or '').strip()):
        _tag_cache[key] = sha
        _write_disk_entry(key, sha)
        return sha
    if blocked is not None and (cached := _read_disk_store().get(key)):
        log.warning('Using disk-cached GitHub commit SHA `%s` for `%s/%s@%s` after HTTP %d.',
                    cached, owner, repo, ref, blocked)
        _tag_cache[key] = cached
        return cached
    msg = f'Could not get commit SHA for `{owner}/{repo}@{ref}`.'
    raise ValueError(msg)


_DESIRED_GITHUB_RULESETS: list[Ruleset] = [
    {
        'name':
            'Protect version tags',
        'target':
            'tag',
        'enforcement':
            'active',
        'bypass_actors': [{
            'actor_id': 5,
            'actor_type': 'RepositoryRole',
            'bypass_mode': 'always',
        }],
        'conditions': {
            'ref_name': {
                'exclude': [],
                'include': ['refs/tags/v*']
            }
        },
        'rules': [
            {
                'type': 'deletion'
            },
            {
                'type': 'non_fast_forward'
            },
            {
                'type': 'required_linear_history'
            },
            {
                'type': 'creation'
            },
            {
                'type': 'update'
            },
            {
                'type': 'required_signatures'
            },
        ],
    },
    {
        'name':
            'Protect default branch',
        'target':
            'branch',
        'enforcement':
            'active',
        'bypass_actors': [{
            'actor_id': 5,
            'actor_type': 'RepositoryRole',
            'bypass_mode': 'always',
        }],
        'conditions': {
            'ref_name': {
                'exclude': [],
                'include': ['~DEFAULT_BRANCH']
            }
        },
        'rules': [
            {
                'type': 'deletion'
            },
            {
                'type': 'non_fast_forward'
            },
            {
                'type': 'pull_request',
                'parameters': {
                    'allowed_merge_methods': ['squash', 'rebase'],
                    'dismiss_stale_reviews_on_push': True,
                    'require_code_owner_review': True,
                    'require_last_push_approval': True,
                    'required_approving_review_count': 1,
                    'required_review_thread_resolution': True,
                },
            },
        ],
    },
    {
        'name':
            'Copilot review for default branch',
        'target':
            'branch',
        'enforcement':
            'active',
        'bypass_actors': [{
            'actor_id': 5,
            'actor_type': 'RepositoryRole',
            'bypass_mode': 'always',
        }],
        'conditions': {
            'ref_name': {
                'exclude': [],
                'include': ['~DEFAULT_BRANCH']
            }
        },
        'rules': [
            {
                'type': 'deletion'
            },
            {
                'type': 'copilot_code_review',
                'parameters': {
                    'review_on_push': True,
                    'review_draft_pull_requests': True
                },
            },
        ],
    },
]


def _github_repo_config(*, description: str = '', homepage: str = '') -> RepositoryConfig:
    return {
        'allow_auto_merge': False,
        'allow_merge_commit': False,
        'allow_rebase_merge': True,
        'allow_squash_merge': True,
        'allow_update_branch': True,
        'archived': False,
        'delete_branch_on_merge': True,
        'dependabot_on_actions_enabled': True,
        'dependency_graph_autosubmit_action_enabled': True,
        'dependency_graph_autosubmit_action_use_labeled_runners': False,
        'description': description,
        'enable_max_pushes_checkbox': False,
        'enable_repository_funding_links': True,
        'has_discussions': False,
        'has_downloads': True,
        'has_issues': True,
        'has_pages': True,
        'has_projects': False,
        'has_wiki': False,
        'homepage': homepage,
        'include_lfs_objects': False,
        'security_and_analysis': {
            'dependabot_security_updates': {
                'status': 'enabled'
            },
            'secret_scanning': {
                'status': 'enabled'
            },
            'secret_scanning_non_provider_patterns': {
                'status': 'disabled'
            },
            'secret_scanning_push_protection': {
                'status': 'enabled'
            },
            'secret_scanning_validity_checks': {
                'status': 'disabled'
            },
        },
        'squash_merge_commit_message': 'COMMIT_MESSAGES',
        'squash_merge_commit_title': 'COMMIT_OR_PR_TITLE',
        'use_squash_pr_title_as_default': False,
        'vulnerability_updates_grouping_enabled': True,
        'web_commit_signoff_required': True,
    }


async def _patch_github_repository(api: NiquestsGitHubAPI,
                                   slug: str,
                                   *,
                                   description: str = '',
                                   homepage: str = '') -> None:
    try:
        await api.patch(f'/repos/{slug}',
                        data=dict(_github_repo_config(description=description, homepage=homepage)))
        log.info('Applied GitHub repository settings.')
    except HTTPException as e:
        log.warning('Could not apply GitHub repository settings: %s.', e)


async def _put_github_topics(api: NiquestsGitHubAPI, slug: str, keywords: Iterable[str]) -> None:
    try:
        await api.put(f'/repos/{slug}/topics',
                      data={'names': [k.replace(' ', '-') for k in keywords]})
        log.info('Applied GitHub repository topics.')
    except HTTPException as e:
        log.warning('Could not apply GitHub repository topics: %s.', e)


async def _put_github_security_features(api: NiquestsGitHubAPI, slug: str, *,
                                        immutable_releases: bool) -> None:
    for endpoint in ('automated-security-fixes', 'private-vulnerability-reporting',
                     'vulnerability-alerts'):
        try:
            await api.put(f'/repos/{slug}/{endpoint}', data=b'')
            log.info('Enabled GitHub `%s`.', endpoint)
        except HTTPException as e:  # noqa: PERF203  # one failure must not block the rest.
            log.warning('Could not enable GitHub `%s`: %s.', endpoint, e)
    if immutable_releases:
        try:
            await api.put(f'/repos/{slug}/immutable-releases', data=b'')
            log.info('Enabled GitHub immutable releases.')
        except HTTPException as e:
            log.warning('Could not enable GitHub immutable releases: %s.', e)


async def _put_github_actions_permissions(api: NiquestsGitHubAPI, slug: str, *,
                                          sha_pinning_required: bool) -> None:
    if not sha_pinning_required:
        return
    endpoint = f'/repos/{slug}/actions/permissions'
    try:
        current = dict(await api.getitem(endpoint))
    except HTTPException as e:
        log.warning('Could not read GitHub Actions permissions: %s.', e)
        return
    # PUT replaces the whole resource, so preserve the existing enablement and allow-list rather
    # than resetting a repository that restricts which actions may run.
    body: dict[str, Any] = {'enabled': current.get('enabled', True), 'sha_pinning_required': True}
    if (allowed_actions := current.get('allowed_actions')) is not None:
        body['allowed_actions'] = allowed_actions
    try:
        await api.put(endpoint, data=body)
        log.info('Enabled GitHub Actions SHA pinning requirement.')
    except HTTPException as e:
        log.warning('Could not enable GitHub Actions SHA pinning requirement: %s.', e)


async def _put_github_oidc_subject(api: NiquestsGitHubAPI, slug: str, *,
                                   immutable_oidc_subject: bool) -> None:
    if not immutable_oidc_subject:
        return
    try:
        await api.put(f'/repos/{slug}/actions/oidc/customization/sub',
                      data={
                          'use_default': True,
                          'use_immutable_subject': True
                      })
        log.info('Enabled GitHub immutable OIDC subject claim.')
    except HTTPException as e:
        log.warning('Could not enable GitHub immutable OIDC subject claim: %s.', e)


async def _sync_github_rulesets(api: NiquestsGitHubAPI, slug: str) -> None:
    existing: dict[str, int] = {}
    try:
        async for ruleset in api.getiter(f'/repos/{slug}/rulesets'):
            if (isinstance(ruleset, dict) and isinstance(ruleset.get('name'), str)
                    and isinstance(ruleset.get('id'), int)):
                existing[ruleset['name']] = ruleset['id']
    except HTTPException as e:
        log.warning('Could not list GitHub rulesets: %s.', e)
        return
    for ruleset in _DESIRED_GITHUB_RULESETS:
        name = ruleset['name']
        try:
            if name in existing:
                await api.put(f'/repos/{slug}/rulesets/{existing[name]}', data=dict(ruleset))
            else:
                await api.post(f'/repos/{slug}/rulesets', data=dict(ruleset))
            log.info('Applied GitHub ruleset `%s`.', name)
        except (HTTPException, TypeError, KeyError) as e:
            # gidgethub raises TypeError or KeyError rather than HTTPException when GitHub
            # rejects a ruleset with a 422 whose `errors` payload is a list of strings instead
            # of objects, as the rulesets endpoint does. A single failure must not block the
            # rest.
            log.warning('Could not apply GitHub ruleset `%s`: %s.', name, e)


async def _bootstrap_github_pages(api: NiquestsGitHubAPI, slug: str, default_branch: str) -> None:
    if await get_pages_build_type(api, slug) is not None:
        return
    try:
        await api.post(f'/repos/{slug}/pages',
                       data={'source': {
                           'branch': default_branch,
                           'path': '/'
                       }})
        log.info('Created GitHub Pages site for `%s`.', slug)
    except HTTPException as e:
        log.warning('Could not create GitHub Pages site: %s.', e)


async def configure_project(session: niquests.AsyncSession,
                            *,
                            repository_uri: str,
                            description: str = '',
                            homepage: str = '',
                            keywords: Iterable[str] = (),
                            default_branch: str | None = None,
                            private: bool = False,
                            immutable_releases: bool = False,
                            sha_pinning_required: bool = False,
                            immutable_oidc_subject: bool = False) -> None:
    """
    Configure a GitHub repository's settings, topics, security toggles, rulesets, and Pages.

    Authentication uses the :py:data:`GITHUB_TOKEN_ENV` environment variable first, then the
    system keyring (see :py:func:`get_github_token`). The caller
    is responsible for deciding whether to invoke this function (for example by checking a
    ``using_github`` flag); this routine always attempts to run when called.

    Each sub-operation is wrapped so a single GitHub HTTP failure logs a warning and the rest of
    the flow continues.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session used by the gidgethub adapter.
    repository_uri : str
        HTTPS URI of the GitHub repository (used to derive the API host and ``owner/repo`` slug).
    description : str
        Short repository description applied to the ``PATCH /repos/:slug`` body.
    homepage : str
        External homepage URL applied to the ``PATCH /repos/:slug`` body.
    keywords : Iterable[str]
        Free-form keywords; each entry has spaces replaced with hyphens before being stored as a
        GitHub topic.
    default_branch : str | None
        Branch used as the source for the GitHub Pages site when one is bootstrapped.
    private : bool
        Whether the repository is private; suppresses the GitHub Pages bootstrap when ``True``.
    immutable_releases : bool
        Whether to enable GitHub's immutable releases feature.
    sha_pinning_required : bool
        Whether to require that GitHub Actions are pinned to a full-length commit SHA.
    immutable_oidc_subject : bool
        Whether to opt in to the immutable OIDC subject claim format for the repository.
    """
    host = urlparse(repository_uri).hostname or 'github.com'
    token = get_github_token(host)
    if not token:
        log.warning('No GitHub token (set %s or keyring `wiswa-github:%s`).', GITHUB_TOKEN_ENV,
                    host)
        return
    slug = slug_from_uri(repository_uri)
    api = NiquestsGitHubAPI(session, USER_AGENT, oauth_token=token)
    await _patch_github_repository(api, slug, description=description, homepage=homepage)
    await _put_github_topics(api, slug, keywords)
    await _put_github_security_features(api, slug, immutable_releases=immutable_releases)
    await _put_github_actions_permissions(api, slug, sha_pinning_required=sha_pinning_required)
    await _put_github_oidc_subject(api, slug, immutable_oidc_subject=immutable_oidc_subject)
    await _sync_github_rulesets(api, slug)
    if not private and default_branch:
        await _bootstrap_github_pages(api, slug, default_branch)
