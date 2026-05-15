"""Tests for :py:mod:`wiswa_vcs.commands.sync_gh_gl`."""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from wiswa_vcs.commands.sync_gh_gl import main

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import CliRunner
    from pytest_mock import MockerFixture

_BASE_ARGS = (
    '--default-branch',
    'master',
    '--github-repo-uri',
    'owner/repo',
    '--github-token',
    'gh',
    '--gitlab-repo-uri',
    'https://gitlab.com/g/p.git',
    '--gitlab-token',
    'gl',
)


def _patch_session(mocker: MockerFixture) -> None:
    session_cm = mocker.MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value='session-object')
    session_cm.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('wiswa_vcs.commands.sync_gh_gl.niquests.AsyncSession', return_value=session_cm)


def test_sync_gh_gl_invokes_sync(runner: CliRunner, mocker: MockerFixture, tmp_path: Path) -> None:
    sync = mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(main, [*_BASE_ARGS, '--badges-file', str(tmp_path / 'missing.rst')])
    assert result.exit_code == 0, result.output
    sync.assert_awaited_once()
    assert sync.await_args is not None
    _, kwargs = sync.await_args
    assert kwargs['github_repo_uri'] == 'owner/repo'
    assert kwargs['gitlab_repo_uri'] == 'https://gitlab.com/g/p.git'
    assert kwargs['apply_mirror_overrides'] is True
    assert kwargs['badges_file'] is None
    assert kwargs['gitlab_config'] == {}


def test_sync_gh_gl_passes_badges_file_when_present(runner: CliRunner, mocker: MockerFixture,
                                                    tmp_path: Path) -> None:
    badges = tmp_path / 'badges.rst'
    badges.write_text('', encoding='utf-8')
    sync = mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(main,
                           [*_BASE_ARGS, '--badges-file',
                            str(badges), '--no-mirror-overrides'])
    assert result.exit_code == 0, result.output
    assert sync.await_args is not None
    _, kwargs = sync.await_args
    assert kwargs['badges_file'] == badges
    assert kwargs['apply_mirror_overrides'] is False


def test_sync_gh_gl_decodes_gitlab_config_json(runner: CliRunner, mocker: MockerFixture) -> None:
    sync = mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(
        main, [*_BASE_ARGS, '--gitlab-config', '{"project_settings": {"issues_enabled": "true"}}'])
    assert result.exit_code == 0, result.output
    assert sync.await_args is not None
    _, kwargs = sync.await_args
    assert kwargs['gitlab_config'] == {'project_settings': {'issues_enabled': 'true'}}


def test_sync_gh_gl_rejects_invalid_gitlab_config_json(runner: CliRunner,
                                                       mocker: MockerFixture) -> None:
    mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(main, [*_BASE_ARGS, '--gitlab-config', 'not json'])
    assert result.exit_code != 0
    assert 'decode' in result.output.lower() or 'invalid' in result.output.lower()


def test_sync_gh_gl_rejects_non_object_gitlab_config_json(runner: CliRunner,
                                                          mocker: MockerFixture) -> None:
    mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(main, [*_BASE_ARGS, '--gitlab-config', '[1, 2]'])
    assert result.exit_code != 0
    assert 'json object' in result.output.lower() or 'invalid' in result.output.lower()


def test_sync_gh_gl_aborts_on_sync_failure(runner: CliRunner, mocker: MockerFixture) -> None:
    mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab',
                 new=AsyncMock(side_effect=RuntimeError('boom')))
    _patch_session(mocker)
    result = runner.invoke(main, list(_BASE_ARGS))
    assert result.exit_code != 0


def test_sync_gh_gl_reads_env_vars(runner: CliRunner, mocker: MockerFixture) -> None:
    sync = mocker.patch('wiswa_vcs.commands.sync_gh_gl.sync_github_to_gitlab', new=AsyncMock())
    _patch_session(mocker)
    result = runner.invoke(main, [],
                           env={
                               'DEFAULT_BRANCH': 'main',
                               'GH_TOKEN': 'gh',
                               'GITHUB_REPO_URI': 'owner/repo',
                               'GITLAB_CONFIG_JSON': '{"push_rules": {"prevent_secrets": "true"}}',
                               'GITLAB_REPO_URI': 'https://gitlab.com/g/p.git',
                               'GITLAB_TOKEN': 'gl',
                           })
    assert result.exit_code == 0, result.output
    assert sync.await_args is not None
    _, kwargs = sync.await_args
    assert kwargs['default_branch'] == 'main'
    assert kwargs['gitlab_config'] == {'push_rules': {'prevent_secrets': 'true'}}
