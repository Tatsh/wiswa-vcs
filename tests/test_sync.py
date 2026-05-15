"""Tests for :py:mod:`wiswa.vcs.sync`."""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from wiswa.vcs.sync import sync_github_to_gitlab
import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def _patch_clients(mocker: MockerFixture) -> tuple[MagicMock, MagicMock]:
    gh_api = MagicMock(name='NiquestsGitHubAPI')
    gl_api = MagicMock(name='NiquestsGitLabAPI')
    mocker.patch('wiswa.vcs.sync.github_api.NiquestsGitHubAPI', return_value=gh_api)
    mocker.patch('wiswa.vcs.sync.gitlab_api.NiquestsGitLabAPI', return_value=gl_api)
    return gh_api, gl_api


@pytest.mark.asyncio
async def test_sync_github_to_gitlab_merges_metadata_and_applies_mirror_overrides(
        mocker: MockerFixture, tmp_path: Path) -> None:
    badges_file = tmp_path / 'badges.rst'
    badges_file.write_text('.. image:: https://img/x\n   :target: https://t\n   :alt: A\n',
                           encoding='utf-8')
    _gh_api, gl_api = _patch_clients(mocker)
    mocker.patch('wiswa.vcs.sync.github_api.fetch_repository',
                 new=AsyncMock(return_value={
                     'description': 'd',
                     'homepage': 'https://h',
                     'topics': ['x']
                 }))
    mocker.patch('wiswa.vcs.sync.github_api.protected_branch_names',
                 new=AsyncMock(return_value={'main'}))
    mocker.patch('wiswa.vcs.sync.github_api.protected_tag_patterns',
                 new=AsyncMock(return_value={'v*'}))
    apply = mocker.patch('wiswa.vcs.sync.gitlab_api.apply_project_settings', new=AsyncMock())
    protect_branches = mocker.patch('wiswa.vcs.sync.gitlab_api.protect_branches', new=AsyncMock())
    protect_tags = mocker.patch('wiswa.vcs.sync.gitlab_api.protect_tags', new=AsyncMock())
    sync_badges = mocker.patch('wiswa.vcs.sync.gitlab_api.sync_badges', new=AsyncMock())
    housekeeping = mocker.patch('wiswa.vcs.sync.gitlab_api.trigger_housekeeping', new=AsyncMock())
    await sync_github_to_gitlab(MagicMock(),
                                badges_file=badges_file,
                                default_branch='trunk',
                                github_repo_uri='https://github.com/owner/repo.git',
                                github_token='gh',
                                gitlab_config={'project_settings': {
                                    'issues_enabled': 'true'
                                }},
                                gitlab_repo_uri='https://gitlab.com/group/project.git',
                                gitlab_token='gl')
    assert apply.await_args is not None
    apply_args, apply_kwargs = apply.await_args
    assert apply_args[0] is gl_api
    assert apply_args[1] == 'group%2Fproject'
    project_settings = apply_kwargs['project_settings']
    assert project_settings['description'] == 'd'
    assert project_settings['topics'] == ['x']
    assert project_settings['homepage_url'] == 'https://h'
    assert project_settings['issues_enabled'] == 'true'
    assert project_settings['builds_access_level'] == 'disabled'
    assert project_settings['merge_requests_access_level'] == 'disabled'
    assert project_settings['lfs_enabled'] == 'false'
    assert project_settings['service_desk_enabled'] == 'false'
    assert protect_branches.await_args is not None
    branch_args, _ = protect_branches.await_args
    assert branch_args[2] == {'main', 'trunk'}
    protect_tags.assert_awaited_once_with(gl_api, 'group%2Fproject', {'v*'})
    sync_badges.assert_awaited_once()
    housekeeping.assert_awaited_once_with(gl_api, 'group%2Fproject')


@pytest.mark.asyncio
async def test_sync_github_to_gitlab_skips_mirror_overrides_and_omits_homepage_when_absent(
        mocker: MockerFixture) -> None:
    _patch_clients(mocker)
    mocker.patch('wiswa.vcs.sync.github_api.fetch_repository',
                 new=AsyncMock(return_value={'description': 'd'}))
    mocker.patch('wiswa.vcs.sync.github_api.protected_branch_names',
                 new=AsyncMock(return_value=set()))
    mocker.patch('wiswa.vcs.sync.github_api.protected_tag_patterns',
                 new=AsyncMock(return_value=set()))
    apply = mocker.patch('wiswa.vcs.sync.gitlab_api.apply_project_settings', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.protect_branches', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.protect_tags', new=AsyncMock())
    sync_badges = mocker.patch('wiswa.vcs.sync.gitlab_api.sync_badges', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.trigger_housekeeping', new=AsyncMock())
    await sync_github_to_gitlab(MagicMock(),
                                apply_mirror_overrides=False,
                                default_branch='master',
                                github_repo_uri='owner/repo',
                                github_token='gh',
                                gitlab_repo_uri='https://gitlab.com/g/p.git',
                                gitlab_token='gl')
    assert apply.await_args is not None
    _, apply_kwargs = apply.await_args
    project_settings = apply_kwargs['project_settings']
    assert 'homepage_url' not in project_settings
    assert 'builds_access_level' not in project_settings
    assert project_settings['topics'] == []
    sync_badges.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_github_to_gitlab_skips_badges_when_file_missing(mocker: MockerFixture,
                                                                    tmp_path: Path) -> None:
    _patch_clients(mocker)
    mocker.patch('wiswa.vcs.sync.github_api.fetch_repository', new=AsyncMock(return_value={}))
    mocker.patch('wiswa.vcs.sync.github_api.protected_branch_names',
                 new=AsyncMock(return_value=set()))
    mocker.patch('wiswa.vcs.sync.github_api.protected_tag_patterns',
                 new=AsyncMock(return_value=set()))
    mocker.patch('wiswa.vcs.sync.gitlab_api.apply_project_settings', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.protect_branches', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.protect_tags', new=AsyncMock())
    sync_badges = mocker.patch('wiswa.vcs.sync.gitlab_api.sync_badges', new=AsyncMock())
    mocker.patch('wiswa.vcs.sync.gitlab_api.trigger_housekeeping', new=AsyncMock())
    await sync_github_to_gitlab(MagicMock(),
                                badges_file=tmp_path / 'missing.rst',
                                default_branch='master',
                                github_repo_uri='owner/repo',
                                github_token='gh',
                                gitlab_repo_uri='https://gitlab.com/g/p.git',
                                gitlab_token='gl')
    sync_badges.assert_not_awaited()
