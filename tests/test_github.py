"""Tests for :py:mod:`wiswa.vcs.github`."""
from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from gidgethub import HTTPException
from wiswa.vcs.github import (
    NiquestsGitHubAPI,
    fetch_repository,
    protected_branch_names,
    protected_tag_patterns,
    slug_from_uri,
)
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pytest_mock import MockerFixture


async def _aiter(items: list[Any]) -> AsyncIterator[Any]:  # noqa: RUF029
    for item in items:
        yield item


@pytest.mark.parametrize(('uri', 'expected'), [
    ('https://github.com/owner/repo.git', 'owner/repo'),
    ('https://github.com/owner/repo', 'owner/repo'),
    ('owner/repo', 'owner/repo'),
    ('owner/repo.git', 'owner/repo'),
    ('/owner/repo.git', 'owner/repo'),
])
def test_slug_from_uri_variants(uri: str, expected: str) -> None:
    assert slug_from_uri(uri) == expected


@pytest.mark.asyncio
async def test_niquests_github_api_request_returns_status_headers_body() -> None:
    response = MagicMock()
    response.status_code = 200
    response.headers = {'Content-Type': 'application/json'}
    response.content = b'{}'
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    api = NiquestsGitHubAPI(session, 'wiswa-vcs', oauth_token='tok')
    status, headers, body = await api._request(  # noqa: SLF001
        'GET', 'https://api.github.com/x', {'Accept': 'application/vnd.github+json'})
    assert status == 200
    assert dict(headers) == {'Content-Type': 'application/json'}
    assert body == b'{}'
    session.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_niquests_github_api_request_sends_body_as_data() -> None:
    response = MagicMock(status_code=201, headers={}, content=b'')
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    api = NiquestsGitHubAPI(session, 'wiswa-vcs')
    await api._request('POST', 'https://api.github.com/x', {}, b'payload')  # noqa: SLF001
    assert session.request.await_args is not None
    _, kwargs = session.request.await_args
    assert kwargs['data'] == b'payload'


@pytest.mark.asyncio
async def test_niquests_github_api_request_raises_when_response_incomplete() -> None:
    response = MagicMock(status_code=None, headers={}, content=None)
    session = MagicMock()
    session.request = AsyncMock(return_value=response)
    api = NiquestsGitHubAPI(session, 'wiswa-vcs')
    with pytest.raises(RuntimeError, match='incomplete'):
        await api._request('GET', 'https://api.github.com/x', {})  # noqa: SLF001


@pytest.mark.asyncio
async def test_niquests_github_api_sleep_delegates(mocker: MockerFixture) -> None:
    sleep = mocker.patch('wiswa.vcs.github.asyncio.sleep', new=AsyncMock())
    api = NiquestsGitHubAPI(MagicMock(), 'wiswa-vcs')
    await api.sleep(0.0)
    sleep.assert_awaited_once_with(0.0)


@pytest.mark.asyncio
async def test_fetch_repository_returns_decoded_json() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value={'name': 'repo'})
    result = await fetch_repository(api, 'owner/repo')
    assert result == {'name': 'repo'}
    api.getitem.assert_awaited_once_with('/repos/owner/repo')


@pytest.mark.asyncio
async def test_fetch_repository_propagates_http_exception() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(side_effect=HTTPException(HTTPStatus.NOT_FOUND, 'gone'))
    with pytest.raises(HTTPException):
        await fetch_repository(api, 'owner/repo')


@pytest.mark.asyncio
async def test_protected_branch_names_filters_unprotected() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([
        {
            'name': 'main',
            'protected': True
        },
        {
            'name': 'old',
            'protected': False
        },
        {
            'name': 'release',
            'protected': True
        },
    ]))
    assert await protected_branch_names(api, 'owner/repo') == {'main', 'release'}
    api.getiter.assert_called_once_with('/repos/owner/repo/branches', {'protected': 'true'})


@pytest.mark.asyncio
async def test_protected_branch_names_returns_empty_on_http_exception(
        mocker: MockerFixture) -> None:
    api = MagicMock()
    api.getiter = MagicMock(side_effect=HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, 'boom'))
    warn = mocker.patch('wiswa.vcs.github.log.warning')
    assert await protected_branch_names(api, 'owner/repo') == set()
    warn.assert_called_once()


@pytest.mark.asyncio
async def test_protected_tag_patterns_collects_from_tag_rulesets() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([
        {
            'id': 1,
            'target': 'tag'
        },
        {
            'id': 2,
            'target': 'branch'
        },
        {
            'id': 3,
            'target': 'tag'
        },
    ]))

    async def fake_getitem(url: str) -> dict[str, Any]:  # noqa: RUF029
        if url.endswith('/1'):
            return {'conditions': {'ref_name': {'include': ['refs/tags/v*', 'refs/tags/~ALL', '']}}}
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, 'boom')

    api.getitem = AsyncMock(side_effect=fake_getitem)
    assert await protected_tag_patterns(api, 'owner/repo') == {'v*'}


@pytest.mark.asyncio
async def test_protected_tag_patterns_returns_empty_on_list_error(mocker: MockerFixture) -> None:
    api = MagicMock()
    api.getiter = MagicMock(side_effect=HTTPException(HTTPStatus.FORBIDDEN, 'forbidden'))
    warn = mocker.patch('wiswa.vcs.github.log.warning')
    assert await protected_tag_patterns(api, 'owner/repo') == set()
    warn.assert_called_once()


@pytest.mark.asyncio
async def test_protected_tag_patterns_handles_missing_conditions() -> None:
    api = MagicMock()
    api.getiter = MagicMock(return_value=_aiter([{'id': 1, 'target': 'tag'}]))
    api.getitem = AsyncMock(return_value={})
    assert await protected_tag_patterns(api, 'owner/repo') == set()
