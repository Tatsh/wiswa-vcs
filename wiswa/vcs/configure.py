"""Opinionated GitHub and GitLab project configuration driven by a settings mapping."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse
import logging

from gidgethub import HTTPException as GitHubHTTPException
from gidgetlab.exceptions import HTTPException as GitLabHTTPException

from .auth import GITHUB_TOKEN_ENV, GITLAB_TOKEN_ENV, get_github_token, get_gitlab_token
from .github import (
    USER_AGENT as GITHUB_USER_AGENT,
    NiquestsGitHubAPI,
    get_pages_build_type,
    slug_from_uri,
)
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

__all__ = (
    'configure_github_project',
    'configure_gitlab_project',
    'gitlab_merged_remote_tables',
)

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
    except GitLabHTTPException as e:
        log.warning('Caught error updating GitLab project: %s.', e)
        log.debug('%r', e)


# TODO(wiswa-typing): once published, replace the inline list[dict[str, Any]] with
# list[wiswa.typing.github.Ruleset] (a TypedDict covering name, target, enforcement,
# bypass_actors, conditions, and rules).
_DESIRED_GITHUB_RULESETS: list[dict[str, Any]] = [
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


# TODO(wiswa-typing): once published, replace dict[str, object] with
# wiswa.typing.github.RepositoryConfig (a TypedDict covering the PATCH /repos/:slug body).
def _github_repo_config(settings: Settings) -> dict[str, object]:
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
        'description': settings.get('description', ''),
        'enable_max_pushes_checkbox': False,
        'enable_repository_funding_links': True,
        'has_discussions': False,
        'has_downloads': True,
        'has_issues': True,
        'has_pages': True,
        'has_projects': False,
        'has_wiki': False,
        'homepage': settings.get('homepage', ''),
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


async def _patch_github_repository(api: NiquestsGitHubAPI, slug: str, settings: Settings) -> None:
    try:
        await api.patch(f'/repos/{slug}', data=_github_repo_config(settings))
        log.info('Applied GitHub repository settings.')
    except GitHubHTTPException as e:
        log.warning('Could not apply GitHub repository settings: %s.', e)


async def _put_github_topics(api: NiquestsGitHubAPI, slug: str, keywords: list[str]) -> None:
    try:
        await api.put(f'/repos/{slug}/topics',
                      data={'names': [k.replace(' ', '-') for k in keywords]})
        log.info('Applied GitHub repository topics.')
    except GitHubHTTPException as e:
        log.warning('Could not apply GitHub repository topics: %s.', e)


async def _put_github_security_features(api: NiquestsGitHubAPI, slug: str, *,
                                        immutable_releases: bool) -> None:
    for endpoint in ('automated-security-fixes', 'private-vulnerability-reporting',
                     'vulnerability-alerts'):
        try:
            await api.put(f'/repos/{slug}/{endpoint}', data=b'')
            log.info('Enabled GitHub `%s`.', endpoint)
        except GitHubHTTPException as e:
            log.warning('Could not enable GitHub `%s`: %s.', endpoint, e)
    if immutable_releases:
        try:
            await api.put(f'/repos/{slug}/immutable-releases', data=b'')
            log.info('Enabled GitHub immutable releases.')
        except GitHubHTTPException as e:
            log.warning('Could not enable GitHub immutable releases: %s.', e)


async def _sync_github_rulesets(api: NiquestsGitHubAPI, slug: str) -> None:
    existing: dict[str, int] = {}
    try:
        async for ruleset in api.getiter(f'/repos/{slug}/rulesets'):
            if (isinstance(ruleset, dict) and isinstance(ruleset.get('name'), str)
                    and isinstance(ruleset.get('id'), int)):
                existing[ruleset['name']] = ruleset['id']
    except GitHubHTTPException as e:
        log.warning('Could not list GitHub rulesets: %s.', e)
        return
    for ruleset in _DESIRED_GITHUB_RULESETS:
        name = cast('str', ruleset['name'])
        try:
            if name in existing:
                await api.put(f'/repos/{slug}/rulesets/{existing[name]}', data=ruleset)
            else:
                await api.post(f'/repos/{slug}/rulesets', data=ruleset)
            log.info('Applied GitHub ruleset `%s`.', name)
        except GitHubHTTPException as e:
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
    except GitHubHTTPException as e:
        log.warning('Could not create GitHub Pages site: %s.', e)


async def configure_github_project(session: niquests.AsyncSession, settings: Settings) -> None:
    """
    Configure a GitHub repository's settings, topics, security toggles, rulesets, and Pages.

    Authentication uses the :py:data:`~wiswa.vcs.auth.GITHUB_TOKEN_ENV` environment variable
    first, then the system keyring (see :py:func:`~wiswa.vcs.auth.get_github_token`).

    Skipped silently when ``settings['using_github']`` is falsy or no token is available.
    Each sub-operation is wrapped so a single GitHub HTTP failure logs a warning and the
    rest of the flow continues, matching the behaviour of Wiswa's original
    ``setup_github_project``.

    Parameters
    ----------
    session : niquests.AsyncSession
        Open async HTTP session used by the gidgethub adapter.
    settings : wiswa.typing.Settings
        Merged settings mapping. Reads ``repository_uri``, ``description``, ``homepage``,
        ``keywords``, ``default_branch``, ``private``, and ``github.immutable_releases``.
    """
    if not settings.get('using_github'):
        log.debug('Not running GitHub setup.')
        return
    host = urlparse(settings['repository_uri']).hostname or 'github.com'
    token = get_github_token(host)
    if not token:
        log.warning('No GitHub token (set %s or keyring `wiswa-github:%s`).', GITHUB_TOKEN_ENV,
                    host)
        return
    slug = slug_from_uri(settings['repository_uri'])
    api = NiquestsGitHubAPI(session, GITHUB_USER_AGENT, oauth_token=token)
    immutable_releases = bool(settings['github'].get('immutable_releases', False))
    await _patch_github_repository(api, slug, settings)
    await _put_github_topics(api, slug, list(settings.get('keywords') or []))
    await _put_github_security_features(api, slug, immutable_releases=immutable_releases)
    await _sync_github_rulesets(api, slug)
    if not settings.get('private', False):
        default_branch = settings.get('default_branch')
        if default_branch:
            await _bootstrap_github_pages(api, slug, default_branch)
