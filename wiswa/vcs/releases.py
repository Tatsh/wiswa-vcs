"""
GitHub release-tag and commit-SHA lookups, shared with Wiswa via the cache directory.

Tag and SHA results are cached in process for the lifetime of the run and persisted to
``platformdirs.user_cache_path('wiswa') / 'github_tag_cache.json'`` so the cache is shared
with Wiswa itself. When the GitHub API responds with ``403`` or ``429`` (rate-limited), the
disk cache is consulted as a last-resort fallback.

The disk cache deliberately uses the ``wiswa`` directory (not ``wiswa-vcs``) so a user with
Wiswa already installed does not pay a cold-cache cost the first time they invoke
:py:mod:`wiswa.vcs`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast
import json
import logging
import re

import platformdirs

if TYPE_CHECKING:
    from pathlib import Path

    import niquests

__all__ = ('latest_release_tag', 'ref_commit_sha', 'resolve_changelog_urls')

log = logging.getLogger(__name__)

_GITHUB_TAG_DISK_FILENAME = 'github_tag_cache.json'
_GITHUB_RELEASES_PAGE_CAP = 20
_GITHUB_RELEASES_PER_PAGE = 100

CHANGELOG_KEEP_A_CHANGELOG_FALLBACK_URL = 'https://keepachangelog.com/en/1.1.0/'
"""Fallback URL for the Keep a Changelog specification when GitHub lookup fails.

:meta hide-value:
"""
CHANGELOG_SEMVER_SPEC_FALLBACK_URL = 'https://semver.org/spec/v2.0.0.html'
"""Fallback URL for the Semantic Versioning specification when GitHub lookup fails.

:meta hide-value:
"""

_cache: dict[str, str] = {}
_disk_store_memo_box: list[dict[str, str] | None] = [None]


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


def clear_cache() -> None:
    """
    Drop the in-process tag/SHA cache and the in-memory snapshot of the disk store.

    Does not delete the on-disk cache file under
    ``platformdirs.user_cache_path('wiswa')``; that file is only consulted when GitHub
    responds with ``403`` or ``429``. Intended for tests and long-lived processes that need
    a fresh view of GitHub.
    """
    _cache.clear()
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
    if key in _cache:
        return _cache[key]
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
            _cache[key] = cached
            return cached
        msg = f'Could not get latest tag for `{owner}/{repo}`.'
        raise ValueError(msg)
    _cache[key] = version
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
    if key in _cache:
        return _cache[key]
    resp = await session.get(f'https://api.github.com/repos/{owner}/{repo}/commits/{ref}',
                             headers={'Accept': 'application/vnd.github.sha'},
                             timeout=15)
    blocked = _blocked_status(resp)
    if resp.ok and (sha := (resp.text or '').strip()):
        _cache[key] = sha
        _write_disk_entry(key, sha)
        return sha
    if blocked is not None and (cached := _read_disk_store().get(key)):
        log.warning('Using disk-cached GitHub commit SHA `%s` for `%s/%s@%s` after HTTP %d.',
                    cached, owner, repo, ref, blocked)
        _cache[key] = cached
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
