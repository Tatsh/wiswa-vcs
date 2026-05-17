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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse
import asyncio
import json
import logging
import re

from gidgethub import HTTPException, abc as gh_abc
from typing_extensions import override
import platformdirs

from . import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from wiswa.typing import github as gh_types
    import niquests

__all__ = (
    'CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL',
    'CHANGELOG_SEMVER_SPEC_FALLBACK_URL',
    'GITHUB_API_HEADERS',
    'USER_AGENT',
    'NiquestsGitHubAPI',
    'clear_tag_cache',
    'fetch_repository',
    'get_pages_build_type',
    'latest_release_tag',
    'protected_branch_names',
    'protected_tag_patterns',
    'ref_commit_sha',
    'resolve_changelog_urls',
    'slug_from_uri',
)

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
CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL = 'https://keepachangelog.com/en/1.1.0/'
"""Fallback URL for the Keep a Changelog specification when GitHub lookup fails.

:meta hide-value:
"""
CHANGELOG_SEMVER_SPEC_FALLBACK_URL = 'https://semver.org/spec/v2.0.0.html'
"""Fallback URL for the Semantic Versioning specification when GitHub lookup fails.

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


async def fetch_repository(api: gh_abc.GitHubAPI, slug: str) -> gh_types.Repository:
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
    wiswa.typing.github.Repository
        Decoded JSON body from ``GET /repos/{slug}``.
    """
    return cast('gh_types.Repository', dict(await api.getitem(f'/repos/{slug}')))


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


# TODO(wiswa-typing): swap inline Literal for wiswa.typing.github.PagesBuildType once a
# wiswa-typing release publishes it.
async def get_pages_build_type(api: gh_abc.GitHubAPI,
                               slug: str) -> Literal['legacy', 'workflow'] | None:
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
    Literal['legacy', 'workflow'] | None
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
        return cast('Literal["legacy", "workflow"]', build_type)
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        store = _read_disk_store()
        store[key] = value
        text = f'{json.dumps(store, indent=2, sort_keys=True)}\n'
        tmp = path.with_suffix(f'{path.suffix}.tmp')
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


def _tag_allowed_for_policy(tag: str, *, allow_suffixes: bool, owner: str, repo: str) -> bool:
    if owner == 'google' and repo == 'yapf':
        if not tag.startswith('v'):
            return False
        return allow_suffixes or bool(re.search(r'\d$', tag))
    if not allow_suffixes:
        return tag.startswith('v') and bool(re.search(r'\d$', tag))
    return True


async def _newest_release_tag_before_cutoff(session: niquests.AsyncSession, owner: str, repo: str,
                                            *, cutoff: datetime,
                                            allow_suffixes: bool) -> tuple[str | None, int | None]:
    from packaging.version import Version  # noqa: PLC0415

    best: tuple[Version, str] | None = None
    for page in range(1, _GITHUB_RELEASES_PAGE_CAP + 1):
        resp = await session.get(
            f'https://api.github.com/repos/{owner}/{repo}/releases'
            f'?per_page={_GITHUB_RELEASES_PER_PAGE}&page={page}',
            timeout=15)
        if (status := _blocked_status(resp)) is not None:
            return None, status
        if not resp.ok:
            break
        batch = resp.json()
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
                    tag, allow_suffixes=allow_suffixes, owner=owner, repo=repo):
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
        (filters out things like ``v1.0-beta``). The ``google/yapf`` repository is always
        constrained to ``v``-prefixed tags regardless of this flag.
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
    if min_release_age_minutes is not None:
        key += f'_minage{min_release_age_minutes}'
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
                                                                allow_suffixes=allow_suffixes)
        if status is not None:
            blocked_status = status
        if gated:
            version = gated
        else:
            log.debug(
                'No GitHub release for `%s/%s` predates the %d-minute age gate; falling back.',
                owner, repo, min_release_age_minutes)
    if not version and not skip_releases:
        resp = await session.get(f'https://api.github.com/repos/{owner}/{repo}/releases/latest',
                                 timeout=15)
        if (status := _blocked_status(resp)) is not None:
            blocked_status = status
        if resp.ok:
            version = resp.json().get('tag_name')
    if not version:
        resp = await session.get(f'https://api.github.com/repos/{owner}/{repo}/tags', timeout=15)
        if (status := _blocked_status(resp)) is not None:
            blocked_status = status
        if resp.ok:
            tags = [x['name'] for x in resp.json() if 'name' in x]
            if tags:
                if not allow_suffixes or (owner == 'google' and repo == 'yapf'):
                    version = next((t for t in tags if t.startswith('v') and (
                        re.search(r'\d$', t) if not allow_suffixes else True)), None)
                else:
                    version = tags[0]
    if not version:
        if blocked_status is not None and (cached := _read_disk_store().get(key)):
            log.warning(
                'Using disk-cached GitHub tag `%s` for `%s/%s` after HTTP %d (likely rate-limited).',
                cached, owner, repo, blocked_status)
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
    resp = await session.get(f'https://api.github.com/repos/{owner}/{repo}/commits/{ref}',
                             headers={'Accept': 'application/vnd.github.sha'},
                             timeout=15)
    blocked = _blocked_status(resp)
    if resp.ok and (sha := (resp.text or '').strip()):
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


def _keep_a_changelog_url_for(tag: str) -> str:
    return f'https://keepachangelog.com/en/{tag.strip().removeprefix("v")}/'


def _semver_spec_url_for(tag: str) -> str:
    stripped = tag.strip()
    if not stripped.startswith('v'):
        stripped = f'v{stripped}'
    return f'https://semver.org/spec/{stripped}.html'


async def _keep_a_changelog_url(session: niquests.AsyncSession | None) -> str:
    if session is None:
        return CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL
    try:
        tag = await latest_release_tag(session, 'olivierlacan', 'keep-a-changelog')
    except (KeyError, OSError, TypeError, ValueError) as exc:
        log.warning('Keep a Changelog tag lookup failed (%s); using fallback URL.', exc)
        return CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL
    candidate = _keep_a_changelog_url_for(tag)
    try:
        resp = await session.head(candidate, timeout=10, allow_redirects=True)
    except OSError as exc:
        log.warning('HEAD `%s` failed (%s); using fallback URL.', candidate, exc)
        return CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL
    if getattr(resp, 'ok', False):
        return candidate
    log.warning('Keep a Changelog URL `%s` is not reachable; using fallback `%s`.', candidate,
                CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL)
    return CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL


async def _semver_spec_url(session: niquests.AsyncSession | None) -> str:
    if session is None:
        return CHANGELOG_SEMVER_SPEC_FALLBACK_URL
    try:
        tag = await latest_release_tag(session, 'semver', 'semver')
    except (KeyError, OSError, TypeError, ValueError) as exc:
        log.warning('SemVer spec tag lookup failed (%s); using fallback URL.', exc)
        return CHANGELOG_SEMVER_SPEC_FALLBACK_URL
    return _semver_spec_url_for(tag)


async def resolve_changelog_urls(session: niquests.AsyncSession | None) -> tuple[str, str]:
    """
    Resolve the Keep a Changelog and Semantic Versioning specification URLs.

    Looks up the latest tag of ``olivierlacan/keep-a-changelog`` and ``semver/semver`` and
    builds the canonical ``https://keepachangelog.com/en/<tag>/`` and
    ``https://semver.org/spec/v<tag>.html`` URLs. Falls back to the hardcoded ``1.1.0`` and
    ``v2.0.0`` URLs when *session* is :py:data:`None`, the GitHub lookup fails, or the
    Keep a Changelog URL is not yet reachable on the live site.

    Parameters
    ----------
    session : niquests.AsyncSession | None
        Open async HTTP session, or :py:data:`None` to skip GitHub lookups entirely.

    Returns
    -------
    tuple[str, str]
        ``(keep_a_changelog_url, semver_spec_url)`` in that order.
    """
    return await _keep_a_changelog_url(session), await _semver_spec_url(session)
