"""Tests for :py:mod:`wiswa.vcs.gitlab`."""
from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call
import logging

from gidgetlab.exceptions import BadRequest, HTTPException
from wiswa.vcs.gitlab import (
    GITLAB_TOKEN_ENV,
    MAINTAINER_ACCESS_LEVEL,
    MIRROR_PROJECT_SETTINGS_OVERRIDES,
    NiquestsGitLabAPI,
    apply_project_settings,
    base_url,
    configure_project,
    desired_gitlab_badges,
    encode_project_path,
    fetch_project_default_branch,
    get_gitlab_token,
    gitlab_merged_remote_tables,
    parse_badges,
    patch_protected_branch,
    project_path,
    protect_branches,
    protect_tags,
    repository_uri_hostname,
    sync_badges,
    trigger_housekeeping,
)
from wiswa.vcs.typing import Badge
import keyring.errors
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_mock import MockerFixture
    from wiswa.vcs.typing import RemoteSettings


async def _aiter(items: list[Any]) -> AsyncIterator[Any]:  # noqa: RUF029
    for item in items:
        yield item


_SAMPLE_BADGES_RST = """\
.. only:: html

   .. image:: https://img.shields.io/pypi/v/wiswa
      :target: https://pypi.org/project/wiswa/
      :alt: PyPI - Version

   .. image:: local-only.svg
      :target: https://example.com
      :alt: Skipped because not HTTP

   .. image:: https://img.shields.io/badge/pre--commit-enabled-brightgreen
      :alt: Missing target, skipped

   .. image:: https://img.shields.io/badge/no-alt
      :target: https://example.com
"""


@pytest.mark.parametrize(('uri', 'expected'), [
    ('https://gitlab.com/group/project.git', 'https://gitlab.com'),
    ('https://gitlab.example.com:8080/g/p', 'https://gitlab.example.com:8080'),
])
def test_base_url_strips_path(uri: str, expected: str) -> None:
    assert base_url(uri) == expected


@pytest.mark.parametrize(('uri', 'expected'), [
    ('https://gitlab.com/group/project.git', 'group/project'),
    ('https://gitlab.com/group/sub/project', 'group/sub/project'),
    ('https://gitlab.com/', ''),
])
def test_project_path_strips_git_suffix(uri: str, expected: str) -> None:
    assert project_path(uri) == expected


def test_encode_project_path_escapes_slashes() -> None:
    assert encode_project_path('group/sub/project') == 'group%2Fsub%2Fproject'


def test_mirror_overrides_disable_expected_features() -> None:
    assert MIRROR_PROJECT_SETTINGS_OVERRIDES == {
        'builds_access_level': 'disabled',
        'lfs_enabled': 'false',
        'merge_requests_access_level': 'disabled',
        'service_desk_enabled': 'false',
    }


def test_parse_badges_extracts_valid_entries() -> None:
    assert list(parse_badges(_SAMPLE_BADGES_RST)) == [{
        'image_url': 'https://img.shields.io/pypi/v/wiswa',
        'link_url': 'https://pypi.org/project/wiswa/',
        'name': 'PyPI - Version',
    }]


def test_parse_badges_empty_input() -> None:
    assert list(parse_badges('')) == []


def test_parse_badges_ignores_unknown_options_and_malformed_option_lines() -> None:
    text = ('.. image:: https://img.shields.io/badge/with-extras\n'
            '   :target: https://example.com\n'
            '   :alt: With Extras\n'
            '   :width: 100px\n'
            '   :not-an-option-because-no-trailing-colon\n')
    assert list(parse_badges(text)) == [{
        'image_url': 'https://img.shields.io/badge/with-extras',
        'link_url': 'https://example.com',
        'name': 'With Extras',
    }]


@pytest.mark.asyncio
async def test_niquests_gitlab_api_request_returns_status_headers_body() -> None:
    response = MagicMock(status_code=200, headers={'X': 'Y'}, content=b'{"ok":true}')
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    api = NiquestsGitLabAPI(session,
                            'wiswa-vcs',
                            access_token='tok',
                            url='https://gitlab.example.com')
    status, headers, body = await api._request(  # noqa: SLF001
        'POST', 'https://gitlab.example.com/api/v4/x', {'Accept': 'application/json'}, b'payload')
    assert status == 200
    assert dict(headers) == {'X': 'Y'}
    assert body == b'{"ok":true}'
    assert session.request.await_args is not None
    _, kwargs = session.request.await_args
    assert kwargs['data'] == b'payload'


@pytest.mark.asyncio
async def test_niquests_gitlab_api_request_raises_when_response_incomplete() -> None:
    response = MagicMock(status_code=None, headers={}, content=None)
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    api = NiquestsGitLabAPI(session, 'wiswa-vcs')
    with pytest.raises(RuntimeError, match='incomplete'):
        await api._request('GET', 'https://gitlab.example.com/api/v4/x', {})  # noqa: SLF001


@pytest.mark.asyncio
async def test_niquests_gitlab_api_sleep_delegates(mocker: MockerFixture) -> None:
    sleep = mocker.patch('wiswa.vcs.gitlab.asyncio.sleep', new=AsyncMock())
    api = NiquestsGitLabAPI(MagicMock(), 'wiswa-vcs')
    await api.sleep(1.5)
    sleep.assert_awaited_once_with(1.5)


@pytest.mark.asyncio
async def test_apply_project_settings_puts_settings_only_when_empty_rules() -> None:
    api = MagicMock()
    api.put = AsyncMock()
    api.post = AsyncMock()
    await apply_project_settings(api, 'g%2Fp', project_settings={'description': 'd'})
    api.put.assert_awaited_once_with('/projects/g%2Fp', data={'description': 'd'})
    api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_project_settings_writes_push_rules_and_approvals() -> None:
    api = MagicMock()
    api.put = AsyncMock()
    api.post = AsyncMock()
    await apply_project_settings(api,
                                 'g%2Fp',
                                 project_approvals={'approvals_before_merge': 1},
                                 project_settings={'description': 'd'},
                                 push_rules={'prevent_secrets': 'true'})
    assert api.put.await_args_list == [
        call('/projects/g%2Fp', data={'description': 'd'}),
        call('/projects/g%2Fp/push_rule', data={'prevent_secrets': 'true'}),
    ]
    api.post.assert_awaited_once_with('/projects/g%2Fp/approvals',
                                      data={'approvals_before_merge': 1})


@pytest.mark.asyncio
async def test_apply_project_settings_falls_back_to_post_when_push_rule_put_fails() -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[None, HTTPException(HTTPStatus.NOT_FOUND, 'no push rule')])
    api.post = AsyncMock()
    await apply_project_settings(api,
                                 'g%2Fp',
                                 project_settings={'description': 'd'},
                                 push_rules={'prevent_secrets': 'true'})
    api.post.assert_awaited_once_with('/projects/g%2Fp/push_rule', data={'prevent_secrets': 'true'})


@pytest.mark.asyncio
@pytest.mark.parametrize('detail', [
    'Pages access level is not allowed for the project visibility level',
    {
        'pages_access_level': ['is not allowed for the project visibility level']
    },
])
async def test_apply_project_settings_retries_without_rejected_setting(
        detail: Any, caplog: pytest.LogCaptureFixture) -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[BadRequest(HTTPStatus.BAD_REQUEST, detail), None])
    api.post = AsyncMock()
    with caplog.at_level(logging.WARNING):
        await apply_project_settings(api,
                                     'g%2Fp',
                                     project_settings={
                                         'description': 'd',
                                         'pages_access_level': 'enabled'
                                     })
    assert api.put.await_args_list == [
        call('/projects/g%2Fp', data={
            'description': 'd',
            'pages_access_level': 'enabled'
        }),
        call('/projects/g%2Fp', data={'description': 'd'}),
    ]
    assert '`pages_access_level`' in caplog.text


@pytest.mark.asyncio
async def test_apply_project_settings_drops_only_the_named_setting() -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[
        BadRequest(HTTPStatus.BAD_REQUEST, 'Issues access level is invalid'),
        None,
    ])
    await apply_project_settings(api,
                                 'g%2Fp',
                                 project_settings={
                                     'issues_access_level': 'disabled',
                                     'issues_enabled': 'true'
                                 })
    assert api.put.await_args_list[1] == call('/projects/g%2Fp', data={'issues_enabled': 'true'})


@pytest.mark.asyncio
async def test_apply_project_settings_retries_after_unprocessable_entity() -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[
        BadRequest(HTTPStatus.UNPROCESSABLE_ENTITY, 'Wiki access level is invalid'),
        None,
    ])
    await apply_project_settings(api,
                                 'g%2Fp',
                                 project_settings={
                                     'description': 'd',
                                     'wiki_access_level': 'enabled'
                                 })
    assert api.put.await_args_list[1] == call('/projects/g%2Fp', data={'description': 'd'})


@pytest.mark.asyncio
async def test_apply_project_settings_skips_settings_when_rejection_is_unattributable(
        caplog: pytest.LogCaptureFixture) -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[BadRequest(HTTPStatus.BAD_REQUEST, 'Something went wrong')])
    api.post = AsyncMock()
    with caplog.at_level(logging.WARNING):
        await apply_project_settings(api,
                                     'g%2Fp',
                                     project_approvals={'approvals_before_merge': 1},
                                     project_settings={'description': 'd'})
    api.put.assert_awaited_once()
    assert 'Could not apply GitLab project settings' in caplog.text
    api.post.assert_awaited_once_with('/projects/g%2Fp/approvals',
                                      data={'approvals_before_merge': 1})


@pytest.mark.asyncio
async def test_apply_project_settings_gives_up_when_every_setting_is_rejected(
        caplog: pytest.LogCaptureFixture) -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[BadRequest(HTTPStatus.BAD_REQUEST, 'Description is too long')])
    with caplog.at_level(logging.WARNING):
        await apply_project_settings(api, 'g%2Fp', project_settings={'description': 'd'})
    api.put.assert_awaited_once()
    assert 'No GitLab project settings left to apply' in caplog.text


@pytest.mark.asyncio
async def test_apply_project_settings_propagates_unauthorized() -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=BadRequest(HTTPStatus.UNAUTHORIZED, '401 Unauthorized'))
    with pytest.raises(BadRequest, match='Unauthorized'):
        await apply_project_settings(api, 'g%2Fp', project_settings={'description': 'd'})


@pytest.mark.asyncio
async def test_apply_project_settings_warns_when_push_rules_and_approvals_fail(
        caplog: pytest.LogCaptureFixture) -> None:
    api = MagicMock()
    api.put = AsyncMock(side_effect=[None, HTTPException(HTTPStatus.NOT_FOUND, 'no push rule')])
    api.post = AsyncMock(side_effect=[
        HTTPException(HTTPStatus.NOT_FOUND, 'no push rule'),
        HTTPException(HTTPStatus.FORBIDDEN, 'approvals need Premium'),
    ])
    with caplog.at_level(logging.WARNING):
        await apply_project_settings(api,
                                     'g%2Fp',
                                     project_approvals={'approvals_before_merge': 1},
                                     project_settings={'description': 'd'},
                                     push_rules={'prevent_secrets': 'true'})
    assert 'Could not apply GitLab push rules' in caplog.text
    assert 'Could not apply GitLab project approvals' in caplog.text


@pytest.mark.asyncio
async def test_protect_branches_creates_missing_only() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([{'name': 'main'}, {'name': ''}]))
    api.post = AsyncMock()
    await protect_branches(api,
                           'g%2Fp', ['main', 'release'],
                           overrides={'allow_force_push': 'true'})
    api.post.assert_awaited_once_with('/projects/g%2Fp/protected_branches',
                                      data={
                                          'allow_force_push': 'true',
                                          'merge_access_level': MAINTAINER_ACCESS_LEVEL,
                                          'name': 'release',
                                          'push_access_level': MAINTAINER_ACCESS_LEVEL,
                                      })


@pytest.mark.asyncio
async def test_protect_tags_creates_missing_only() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([{'name': 'v1.*'}, {}]))
    api.post = AsyncMock()
    await protect_tags(api, 'g%2Fp', ['v1.*', 'v2.*'])
    api.post.assert_awaited_once_with('/projects/g%2Fp/protected_tags',
                                      data={
                                          'create_access_level': MAINTAINER_ACCESS_LEVEL,
                                          'name': 'v2.*'
                                      })


@pytest.mark.asyncio
async def test_sync_badges_creates_and_updates_and_skips_in_sync() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([
        {
            'id': 1,
            'name': 'PyPI - Version',
            'kind': 'project',
            'image_url': 'old',
            'link_url': 'old'
        },
        {
            'id': 2,
            'name': 'Unchanged',
            'kind': 'project',
            'image_url': 'img',
            'link_url': 'lnk'
        },
        {
            'id': 3,
            'name': 'GroupBadge',
            'kind': 'group',
            'image_url': 'x',
            'link_url': 'y'
        },
    ]))
    api.put = AsyncMock()
    api.post = AsyncMock()
    desired: list[Badge] = [
        Badge(name='PyPI - Version', image_url='new', link_url='newlnk'),
        Badge(name='Unchanged', image_url='img', link_url='lnk'),
        Badge(name='New One', image_url='i', link_url='l'),
    ]
    await sync_badges(api, 'g%2Fp', desired)
    api.put.assert_awaited_once_with('/projects/g%2Fp/badges/1', data=desired[0])
    api.post.assert_awaited_once_with('/projects/g%2Fp/badges', data=desired[2])


@pytest.mark.asyncio
async def test_trigger_housekeeping_posts_endpoint() -> None:
    api = MagicMock()
    api.post = AsyncMock()
    await trigger_housekeeping(api, 'g%2Fp')
    api.post.assert_awaited_once_with('/projects/g%2Fp/housekeeping', data=None)


@pytest.mark.parametrize(('uri', 'expected'), [
    ('https://gitlab.com/g/p.git', 'gitlab.com'),
    ('https://gitlab.example.com:8080/x/y', 'gitlab.example.com'),
    ('not a uri', ''),
])
def test_repository_uri_hostname_returns_host(uri: str, expected: str) -> None:
    assert repository_uri_hostname(uri) == expected


def test_get_gitlab_token_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITLAB_TOKEN_ENV, 'env-token')
    assert get_gitlab_token('gitlab.com') == 'env-token'


def test_get_gitlab_token_returns_none_for_empty_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITLAB_TOKEN_ENV, raising=False)
    assert get_gitlab_token('') is None


def test_get_gitlab_token_uses_host_scoped_keyring(monkeypatch: pytest.MonkeyPatch,
                                                   mocker: MockerFixture) -> None:
    monkeypatch.delenv(GITLAB_TOKEN_ENV, raising=False)
    mocker.patch('wiswa.vcs.gitlab.getpass.getuser', return_value='alice')

    def fake_get_password(_service: str, user: str) -> str | None:
        return 'user-token' if user == 'alice' else None

    mocker.patch('wiswa.vcs.gitlab.keyring.get_password', side_effect=fake_get_password)
    assert get_gitlab_token('gitlab.example.com') == 'user-token'


def test_get_gitlab_token_falls_back_to_host_username(monkeypatch: pytest.MonkeyPatch,
                                                      mocker: MockerFixture) -> None:
    monkeypatch.delenv(GITLAB_TOKEN_ENV, raising=False)
    mocker.patch('wiswa.vcs.gitlab.getpass.getuser', return_value='alice')

    def fake_get_password(_service: str, user: str) -> str | None:
        return 'host-token' if user == 'gitlab.example.com' else None

    mocker.patch('wiswa.vcs.gitlab.keyring.get_password', side_effect=fake_get_password)
    assert get_gitlab_token('gitlab.example.com') == 'host-token'


def test_get_gitlab_token_returns_none_when_keyring_missing(
        monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv(GITLAB_TOKEN_ENV, raising=False)
    mocker.patch('wiswa.vcs.gitlab.keyring.get_password', side_effect=keyring.errors.NoKeyringError)
    with caplog.at_level(logging.WARNING):
        assert get_gitlab_token('gitlab.example.com') is None
    assert 'No keyring backend' in caplog.text


@pytest.mark.asyncio
async def test_fetch_project_default_branch_returns_default_attribute() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value={'default_branch': 'main'})
    assert await fetch_project_default_branch(api, 'g%2Fp') == 'main'


@pytest.mark.asyncio
async def test_fetch_project_default_branch_handles_non_dict_response() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value='unexpected')
    assert await fetch_project_default_branch(api, 'g%2Fp') is None


@pytest.mark.asyncio
async def test_patch_protected_branch_posts_body() -> None:
    api = MagicMock()
    api.patch = AsyncMock()
    await patch_protected_branch(api, 'g%2Fp', 'main', {'allow_force_push': 'true'})
    api.patch.assert_awaited_once_with('/projects/g%2Fp/protected_branches/main',
                                       data={'allow_force_push': 'true'})


def test_gitlab_merged_remote_tables_handles_none() -> None:
    ps, pr, pa, dbp = gitlab_merged_remote_tables(None)
    assert ps == pr == pa == dbp == {}


def test_gitlab_merged_remote_tables_returns_passthrough_subtables() -> None:
    gitlab: RemoteSettings = {
        'default_branch_protection': {
            'allow_force_push': 'false'
        },
        'project_approvals': {
            'approvals_before_merge': 2
        },
        'project_settings': {
            'issues_enabled': 'false'
        },
        'push_rules': {
            'prevent_secrets': 'false'
        },
    }
    ps, pr, pa, dbp = gitlab_merged_remote_tables(gitlab)
    assert ps == gitlab['project_settings']
    assert pr == gitlab['push_rules']
    assert pa == gitlab['project_approvals']
    assert dbp == gitlab['default_branch_protection']


def _badges_for(**kwargs: Any) -> list[Badge]:
    defaults: dict[str, Any] = {
        'repository_uri': 'https://gitlab.example.com/group/sub/project',
    }
    defaults.update(kwargs)
    return desired_gitlab_badges(**defaults)


def test_desired_gitlab_badges_python_uv_full_set() -> None:
    names = [
        b['name'] for b in _badges_for(want_tests=True, project_type='python', package_manager='uv')
    ]
    assert names == [
        'QA', 'Coverage', 'Latest Release', 'mypy', 'uv', 'pytest', 'Ruff', 'pre-commit', 'Prettier'
    ]


def test_desired_gitlab_badges_python_poetry_replaces_uv() -> None:
    names = [
        b['name']
        for b in _badges_for(want_tests=True, project_type='python', package_manager='poetry')
    ]
    assert 'Poetry' in names
    assert 'uv' not in names


def test_desired_gitlab_badges_no_tests_skips_coverage_and_pytest() -> None:
    names = [b['name'] for b in _badges_for(project_type='python', want_tests=False)]
    assert 'Coverage' not in names
    assert 'pytest' not in names


def test_desired_gitlab_badges_stubs_only_skips_pytest() -> None:
    names = [
        b['name'] for b in _badges_for(project_type='python', want_tests=True, stubs_only=True)
    ]
    assert 'pytest' not in names
    assert 'mypy' in names


def test_desired_gitlab_badges_django_before_mypy() -> None:
    names = [b['name'] for b in _badges_for(project_type='python', using_django=True)]
    assert names.index('Django') < names.index('mypy')


def test_desired_gitlab_badges_non_python_strips_language_specific() -> None:
    names = [b['name'] for b in _badges_for(project_type='typescript', want_tests=True)]
    assert names == ['QA', 'Coverage', 'Latest Release', 'pre-commit', 'Prettier']


def _patch_gitlab_token(mocker: MockerFixture,
                        token: str | None = 'gl-token') -> None:  # noqa: S107
    mocker.patch('wiswa.vcs.gitlab.get_gitlab_token', return_value=token)


def _make_gl_api(mocker: MockerFixture) -> MagicMock:
    api = MagicMock()
    api.put = AsyncMock()
    api.post = AsyncMock()
    api.patch = AsyncMock()
    api.getitem = AsyncMock(return_value={})
    api.getiter = MagicMock(return_value=_aiter([]))
    mocker.patch('wiswa.vcs.gitlab.NiquestsGitLabAPI', return_value=api)
    return api


@pytest.mark.asyncio
async def test_configure_project_returns_when_no_token(mocker: MockerFixture,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    _patch_gitlab_token(mocker, token=None)
    new_api = mocker.patch('wiswa.vcs.gitlab.NiquestsGitLabAPI')
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://gitlab.example.com/group/repo')
    new_api.assert_not_called()
    assert 'No GitLab token' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_runs_full_flow(mocker: MockerFixture) -> None:
    _patch_gitlab_token(mocker)
    api = _make_gl_api(mocker)
    api.getitem = AsyncMock(return_value={'default_branch': 'main'})
    await configure_project(MagicMock(),
                            repository_uri='https://gitlab.example.com/group/sub/repo',
                            description='desc',
                            homepage='https://example.com',
                            keywords=['a', 'multi word'],
                            default_branch='main',
                            gitlab_config={
                                'project_settings': {
                                    'issues_enabled': 'true'
                                },
                                'push_rules': {
                                    'prevent_secrets': 'true'
                                },
                                'project_approvals': {
                                    'approvals_before_merge': 1
                                },
                                'default_branch_protection': {
                                    'allow_force_push': 'false'
                                },
                            },
                            want_tests=True,
                            project_type='python',
                            using_django=False,
                            package_manager='uv',
                            stubs_only=False)
    project_put = next(c for c in api.put.await_args_list
                       if c.args and c.args[0].startswith('/projects/')
                       and 'push_rule' not in c.args[0] and 'protected_branches' not in c.args[0])
    assert project_put.kwargs['data']['description'] == 'desc'
    assert project_put.kwargs['data']['homepage_url'] == 'https://example.com'
    assert project_put.kwargs['data']['topics'] == ['a', 'multi-word']
    assert any('protected_branches' in c.args[0] for c in api.patch.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_warns_when_project_path_cannot_be_derived(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_gitlab_token(mocker)
    new_api = mocker.patch('wiswa.vcs.gitlab.NiquestsGitLabAPI')
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://gitlab.example.com/')
    new_api.assert_not_called()
    assert 'Could not derive' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_swallows_http_exception(mocker: MockerFixture,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    _patch_gitlab_token(mocker)
    api = _make_gl_api(mocker)
    api.put = AsyncMock(side_effect=HTTPException(HTTPStatus.FORBIDDEN, 'forbidden'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://gitlab.example.com/group/repo')
    assert 'updating GitLab project' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_uses_default_branch_fallback_when_api_has_none(
        mocker: MockerFixture) -> None:
    _patch_gitlab_token(mocker)
    api = _make_gl_api(mocker)
    api.getitem = AsyncMock(return_value={})
    await configure_project(
        MagicMock(),
        repository_uri='https://gitlab.example.com/group/repo',
        default_branch='trunk',
        gitlab_config={'default_branch_protection': {
            'allow_force_push': 'false'
        }})
    branch_patch = next(
        c for c in api.patch.await_args_list if c.args and 'protected_branches/trunk' in c.args[0])
    assert branch_patch is not None


@pytest.mark.asyncio
async def test_configure_project_skips_branch_protection_when_overrides_empty(
        mocker: MockerFixture) -> None:
    _patch_gitlab_token(mocker)
    api = _make_gl_api(mocker)
    await configure_project(MagicMock(),
                            repository_uri='https://gitlab.example.com/group/repo',
                            default_branch='main')
    assert not any('protected_branches' in (c.args[0] if c.args else '')
                   for c in api.patch.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_logs_when_no_branch_resolvable(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_gitlab_token(mocker)
    api = _make_gl_api(mocker)
    api.getitem = AsyncMock(return_value={})
    with caplog.at_level(logging.WARNING):
        await configure_project(
            MagicMock(),
            repository_uri='https://gitlab.example.com/group/repo',
            default_branch=None,
            gitlab_config={'default_branch_protection': {
                'allow_force_push': 'false'
            }})
    assert 'Could not determine default branch' in caplog.text
    assert not any('protected_branches/' in (c.args[0] if c.args else '')
                   for c in api.patch.await_args_list)
