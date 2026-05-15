"""Tests for :py:mod:`wiswa.vcs.gitlab`."""
from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call

from gidgetlab.exceptions import HTTPException
from wiswa.vcs.gitlab import (
    MAINTAINER_ACCESS_LEVEL,
    MIRROR_PROJECT_SETTINGS_OVERRIDES,
    NiquestsGitLabAPI,
    apply_project_settings,
    base_url,
    encode_project_path,
    parse_badges,
    project_path,
    protect_branches,
    protect_tags,
    sync_badges,
    trigger_housekeeping,
)
from wiswa.vcs.typing import Badge
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_mock import MockerFixture


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
