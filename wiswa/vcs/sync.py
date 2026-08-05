"""High-level cross-host synchronisation flows."""
from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
import logging

from gidgetlab.exceptions import BadRequest
import anyio

from . import github as github_api, gitlab as gitlab_api

if TYPE_CHECKING:
    from collections.abc import Awaitable
    import os

    import niquests

    from .typing import ProjectSettings, RemoteSettings, Repository

__all__ = ('sync_github_to_gitlab',)

log = logging.getLogger(__name__)

_TOLERATED_STATUSES = frozenset({
    HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND,
    HTTPStatus.UNPROCESSABLE_ENTITY
})


async def _attempt(action: str, awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except BadRequest as e:
        if e.status_code not in _TOLERATED_STATUSES:
            raise
        log.warning('Could not %s: %s.', action, e)


def _merge_project_settings(base: ProjectSettings | None, github_repo: Repository, *,
                            apply_mirror_overrides: bool) -> ProjectSettings:
    settings: ProjectSettings = base.copy() if base is not None else {}
    settings['description'] = github_repo.get('description') or ''
    settings['topics'] = list(github_repo.get('topics') or [])
    if homepage := github_repo.get('homepage'):
        settings['homepage_url'] = homepage
    if apply_mirror_overrides:
        settings.update(gitlab_api.MIRROR_PROJECT_SETTINGS_OVERRIDES)
    return settings


async def sync_github_to_gitlab(session: niquests.AsyncSession,
                                *,
                                github_repo_uri: str,
                                github_token: str,
                                gitlab_repo_uri: str,
                                gitlab_token: str,
                                default_branch: str,
                                gitlab_config: RemoteSettings | None = None,
                                badges_file: anyio.Path | os.PathLike[str] | None = None,
                                apply_mirror_overrides: bool = True) -> None:
    """
    Synchronise GitHub metadata, protected refs, and badges to a GitLab mirror.

    Each GitLab step is independent: when GitLab refuses branch protection, tag protection, badge
    synchronisation, or housekeeping with a ``400``, ``403``, ``404``, or ``422``, the refusal is
    logged and the remaining steps still run. Rejected project settings are dropped individually
    by :py:func:`~wiswa.vcs.gitlab.apply_project_settings`. Any other error, such as an unusable
    token, propagates to the caller.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session shared by the GitHub and GitLab clients.
    github_repo_uri : str
        Source GitHub repository URI (for example ``https://github.com/owner/repo.git``) or
        bare ``owner/repo`` slug.
    github_token : str
        GitHub personal access token.
    gitlab_repo_uri : str
        HTTPS URI of the destination GitLab repository.
    gitlab_token : str
        GitLab personal access token with the ``api`` scope.
    default_branch : str
        Default branch name; always added to the protected branches list.
    gitlab_config : RemoteSettings | None
        Opinionated GitLab tables (``project_settings``, ``push_rules``, ``project_approvals``,
        ``default_branch_protection``). Empty dictionaries are treated as absent.
    badges_file : anyio.Path | os.PathLike[str] | None
        Path to a ``docs/badges.rst``-style file. Skipped when the file does not exist. An
        :py:class:`anyio.Path` is used as-is; anything else implementing
        :py:class:`os.PathLike` is wrapped in :py:class:`anyio.Path` for the file read.
    apply_mirror_overrides : bool
        When ``True`` (the default), apply
        :py:data:`wiswa.vcs.gitlab.MIRROR_PROJECT_SETTINGS_OVERRIDES` so the GitLab project
        behaves as a read-only mirror.
    """
    gh = github_api.NiquestsGitHubAPI(session, github_api.USER_AGENT, oauth_token=github_token)
    slug = github_api.slug_from_uri(github_repo_uri)
    github_repo = await github_api.fetch_repository(gh, slug)
    protected_branches = await github_api.protected_branch_names(gh, slug)
    protected_branches.add(default_branch)
    tag_patterns = await github_api.protected_tag_patterns(gh, slug)
    badges_text: str | None = None
    if badges_file is not None:
        async_path = (badges_file
                      if isinstance(badges_file, anyio.Path) else anyio.Path(badges_file))
        if await async_path.is_file():
            badges_text = await async_path.read_text(encoding='utf-8')
    config = gitlab_config or {}
    project_settings = _merge_project_settings(config.get('project_settings'),
                                               github_repo,
                                               apply_mirror_overrides=apply_mirror_overrides)
    encoded = gitlab_api.encode_project_path(gitlab_api.project_path(gitlab_repo_uri))
    gl = gitlab_api.NiquestsGitLabAPI(session,
                                      gitlab_api.USER_AGENT,
                                      access_token=gitlab_token,
                                      url=gitlab_api.base_url(gitlab_repo_uri))
    await gitlab_api.apply_project_settings(gl,
                                            encoded,
                                            project_approvals=config.get('project_approvals'),
                                            project_settings=project_settings,
                                            push_rules=config.get('push_rules'))
    await _attempt(
        'protect GitLab branches',
        gitlab_api.protect_branches(gl,
                                    encoded,
                                    protected_branches,
                                    overrides=config.get('default_branch_protection')))
    await _attempt('protect GitLab tags', gitlab_api.protect_tags(gl, encoded, tag_patterns))
    if badges_text is not None:
        await _attempt('sync GitLab badges',
                       gitlab_api.sync_badges(gl, encoded, gitlab_api.parse_badges(badges_text)))
    await _attempt('trigger GitLab housekeeping', gitlab_api.trigger_housekeeping(gl, encoded))
