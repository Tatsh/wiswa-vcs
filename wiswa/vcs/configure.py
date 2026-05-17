"""Opinionated GitLab project configuration driven by a settings mapping."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast
import logging

from gidgetlab.exceptions import HTTPException

from .auth import GITLAB_TOKEN_ENV, get_gitlab_token
from .gitlab import (
    USER_AGENT,
    NiquestsGitLabAPI,
    apply_project_settings,
    base_url,
    encode_project_path,
    fetch_project_default_branch,
    patch_protected_branch,
    project_path,
    repository_uri_hostname,
    sync_badges,
)

if TYPE_CHECKING:
    from wiswa.typing import Settings
    from wiswa.typing.gitlab import (
        Badge,
        BranchProtectionOverrides,
        ProjectApprovals,
        ProjectSettings,
        PushRules,
    )
    import niquests

__all__ = ('configure_gitlab_project', 'gitlab_merged_remote_tables')

log = logging.getLogger(__name__)


def gitlab_merged_remote_tables(
    settings: Settings
) -> tuple[ProjectSettings, PushRules, ProjectApprovals, BranchProtectionOverrides]:
    """
    Return the GitLab API tables from a Wiswa settings mapping.

    Defaults and per-field overrides are expected to have been merged at the source (Jsonnet
    layering for Wiswa projects).

    Parameters
    ----------
    settings : wiswa.typing.Settings
        Merged settings mapping. The ``gitlab`` key is consulted for
        ``project_settings``, ``push_rules``, ``project_approvals``, and
        ``default_branch_protection`` subtables.

    Returns
    -------
    tuple[ProjectSettings, PushRules, ProjectApprovals, BranchProtectionOverrides]
        ``(project_settings, push_rules, project_approvals,
        default_branch_protection)`` ready to feed the GitLab REST API.
    """
    glb = settings.get('gitlab') or {}
    return (glb.get('project_settings') or cast('ProjectSettings', {}), glb.get('push_rules')
            or cast('PushRules', {}), glb.get('project_approvals') or cast('ProjectApprovals', {}),
            glb.get('default_branch_protection') or cast('BranchProtectionOverrides', {}))


def _desired_gitlab_badges(settings: Settings) -> list[Badge]:
    """
    Build the list of badges Wiswa manages on a GitLab project.

    Parameters
    ----------
    settings : wiswa.typing.Settings
        Merged settings mapping. Only the server hostname is derived from
        ``repository_uri``; all other path components use GitLab badge placeholders.

    Returns
    -------
    list[Badge]
        Each entry has ``name``, ``image_url``, and ``link_url`` keys suitable for the GitLab
        project badges API.
    """
    base = base_url(settings['repository_uri'])
    project = f'{base}/%{{project_path}}'
    branch = '%{default_branch}'
    pipelines_link = f'{project}/-/pipelines'
    badges: list[Badge] = [{
        'image_url': f'{project}/badges/{branch}/pipeline.svg?ignore_skipped=true',
        'link_url': pipelines_link,
        'name': 'QA',
    }]
    if settings.get('want_tests'):
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
    if settings.get('project_type') == 'python':
        if settings.get('using_django'):
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
        if settings.get('package_manager') == 'uv':
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
        if settings.get('want_tests') and not settings.get('stubs_only'):
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


async def _configure(session: niquests.AsyncSession, settings: Settings, token: str) -> None:
    uri = settings['repository_uri']
    encoded = encode_project_path(project_path(uri))
    if not encoded:
        log.warning('Could not derive GitLab project path from `%s`.', uri)
        return
    api = NiquestsGitLabAPI(session, USER_AGENT, access_token=token, url=base_url(uri))
    project_settings, push_rules, project_approvals, default_branch_protection = (
        gitlab_merged_remote_tables(settings))
    project_settings['description'] = settings.get('description', '')
    project_settings['topics'] = [x.replace(' ', '-') for x in settings.get('keywords') or []]
    project_settings['homepage_url'] = settings.get('homepage', '')
    await apply_project_settings(api,
                                 encoded,
                                 project_approvals=project_approvals or None,
                                 project_settings=project_settings,
                                 push_rules=push_rules or None)
    if default_branch_protection:
        default_branch = (await fetch_project_default_branch(api, encoded)
                          or settings.get('default_branch'))
        if default_branch:
            await patch_protected_branch(api, encoded, default_branch, default_branch_protection)
        else:
            log.warning('Could not determine default branch for `%s`.', uri)
    await sync_badges(api, encoded, _desired_gitlab_badges(settings))


async def configure_gitlab_project(session: niquests.AsyncSession, settings: Settings) -> None:
    """
    Configure a GitLab project (settings, description, topics, badges, protected branch).

    Authentication uses the :py:data:`~wiswa.vcs.auth.GITLAB_TOKEN_ENV` environment variable
    first, then the system keyring (see :py:func:`~wiswa.vcs.auth.get_gitlab_token`).

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session used by the gidgetlab adapter.
    settings : wiswa.typing.Settings
        Merged settings mapping. The ``gitlab`` sub-mapping provides opinionated GitLab API
        tables; other keys (``description``, ``homepage``, ``keywords``, ``default_branch``,
        ``project_type``, ``want_tests``, and badge-builder inputs such as ``using_django``,
        ``package_manager``, and ``stubs_only``) shape the metadata applied to the project.
    """
    if not settings.get('using_gitlab'):
        log.debug('Not running GitLab setup.')
        return
    host = repository_uri_hostname(settings['repository_uri'])
    token = get_gitlab_token(host)
    if not token:
        log.warning('No GitLab token (set %s or keyring `wiswa-gitlab:%s`).', GITLAB_TOKEN_ENV,
                    host)
        return
    try:
        await _configure(session, settings, token)
    except HTTPException as e:
        log.warning('Caught error updating GitLab project: %s.', e)
        log.debug('%r', e)
