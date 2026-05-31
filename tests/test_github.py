"""Tests for :py:mod:`wiswa.vcs.github`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
import json
import logging

from gidgethub import HTTPException
from wiswa.vcs.github import (
    GITHUB_TOKEN_ENV,
    NiquestsGitHubAPI,
    clear_tag_cache,
    configure_project,
    fetch_repository,
    get_github_token,
    get_pages_build_type,
    latest_release_tag,
    protected_branch_names,
    protected_tag_patterns,
    ref_commit_sha,
    slug_from_uri,
)
import keyring.errors
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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


def _make_niquests_response(*,
                            ok: bool = True,
                            status_code: int | None = None,
                            json_data: object = None,
                            text: str = '') -> MagicMock:
    response = MagicMock()
    response.ok = ok
    response.status_code = status_code if status_code is not None else (200 if ok else 404)
    response.text = text
    response.json = MagicMock(return_value=json_data)
    return response


def test_get_github_token_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GITHUB_TOKEN_ENV, 'env-token')
    assert get_github_token('github.com') == 'env-token'


def test_get_github_token_returns_none_for_empty_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    assert get_github_token('') is None


def test_get_github_token_uses_host_scoped_keyring(monkeypatch: pytest.MonkeyPatch,
                                                   mocker: MockerFixture) -> None:
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)

    def fake_get_password(service: str, _user: str) -> str | None:
        return 'host-token' if service == 'wiswa-github:github.com' else None

    mocker.patch('wiswa.vcs.github.keyring.get_password', side_effect=fake_get_password)
    assert get_github_token('github.com') == 'host-token'


def test_get_github_token_falls_back_to_legacy_keyring(monkeypatch: pytest.MonkeyPatch,
                                                       mocker: MockerFixture) -> None:
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)

    def fake_get_password(service: str, _user: str) -> str | None:
        return 'legacy-token' if service == 'tmu-github-api' else None

    mocker.patch('wiswa.vcs.github.keyring.get_password', side_effect=fake_get_password)
    assert get_github_token('github.com') == 'legacy-token'


def test_get_github_token_returns_none_when_keyring_missing(
        monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv(GITHUB_TOKEN_ENV, raising=False)
    mocker.patch('wiswa.vcs.github.keyring.get_password', side_effect=keyring.errors.NoKeyringError)
    with caplog.at_level(logging.WARNING):
        assert get_github_token('github.com') is None
    assert 'No keyring backend' in caplog.text


@pytest.mark.asyncio
async def test_get_pages_build_type_returns_legacy() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value={'build_type': 'legacy'})
    assert await get_pages_build_type(api, 'owner/repo') == 'legacy'


@pytest.mark.asyncio
async def test_get_pages_build_type_returns_workflow() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value={'build_type': 'workflow'})
    assert await get_pages_build_type(api, 'owner/repo') == 'workflow'


@pytest.mark.asyncio
async def test_get_pages_build_type_none_for_non_dict_response() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value=['unexpected'])
    assert await get_pages_build_type(api, 'owner/repo') is None


@pytest.mark.asyncio
async def test_get_pages_build_type_none_for_unknown_value() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(return_value={'build_type': 'other'})
    assert await get_pages_build_type(api, 'owner/repo') is None


@pytest.mark.asyncio
async def test_get_pages_build_type_returns_none_on_http_exception() -> None:
    api = MagicMock()
    api.getitem = AsyncMock(side_effect=HTTPException(HTTPStatus.NOT_FOUND, 'gone'))
    assert await get_pages_build_type(api, 'owner/repo') is None


@pytest.mark.asyncio
async def test_latest_release_tag_from_release() -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v1.2.3'}))
    assert await latest_release_tag(session, 'owner', 'repo') == 'v1.2.3'


@pytest.mark.asyncio
async def test_latest_release_tag_falls_back_to_tags() -> None:
    release_r = _make_niquests_response(ok=False)
    tags_r = _make_niquests_response(ok=True, json_data=[{'name': 'v2.0.0'}, {'name': 'v1.0.0'}])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[release_r, tags_r])
    assert await latest_release_tag(session, 'owner', 'fallback-repo') == 'v2.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_skip_releases_uses_tags() -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data=[{
            'name': 'v5.0.0'
        }]))
    result = await latest_release_tag(session, 'owner', 'tags-only', skip_releases=True)
    assert result == 'v5.0.0'
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_latest_release_tag_actions_no_suffix_picks_digit_suffix() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True,
                                                                 json_data=[{
                                                                     'name': 'v4.0.0-beta'
                                                                 }, {
                                                                     'name': 'v3.0.1'
                                                                 }]))
    result = await latest_release_tag(session,
                                      'owner',
                                      'no-suffix',
                                      skip_releases=True,
                                      allow_suffixes=False)
    assert result == 'v3.0.1'


@pytest.mark.asyncio
async def test_latest_release_tag_actions_no_suffix_skips_non_v_prefix() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True,
                                                                 json_data=[{
                                                                     'name': '4.1.2'
                                                                 }, {
                                                                     'name': 'v3.0.0'
                                                                 }]))
    result = await latest_release_tag(session,
                                      'owner',
                                      'no-v-prefix',
                                      skip_releases=True,
                                      allow_suffixes=False)
    assert result == 'v3.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_require_v_prefix_picks_v_tag() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True,
                                                                 json_data=[{
                                                                     'name': 'release-0.40'
                                                                 }, {
                                                                     'name': 'v0.40.0'
                                                                 }]))
    result = await latest_release_tag(session,
                                      'google',
                                      'yapf',
                                      skip_releases=True,
                                      allow_suffixes=True,
                                      require_v_prefix=True)
    assert result == 'v0.40.0'


@pytest.mark.asyncio
async def test_latest_release_tag_no_tags_raises() -> None:
    release_r = _make_niquests_response(ok=False)
    tags_r = _make_niquests_response(ok=True, json_data=[])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[release_r, tags_r])
    with pytest.raises(ValueError, match='Could not get latest tag'):
        await latest_release_tag(session, 'owner', 'empty')


@pytest.mark.asyncio
async def test_latest_release_tag_both_fail_raises() -> None:
    session = MagicMock()
    session.get = AsyncMock(
        side_effect=[_make_niquests_response(
            ok=False), _make_niquests_response(ok=False)])
    with pytest.raises(ValueError, match='Could not get latest tag'):
        await latest_release_tag(session, 'owner', 'both-fail')


@pytest.mark.asyncio
async def test_latest_release_tag_caches_result_in_process() -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v1.0.0'}))
    one = await latest_release_tag(session, 'owner', 'cached')
    two = await latest_release_tag(session, 'owner', 'cached')
    assert one == two == 'v1.0.0'
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_latest_release_tag_persists_to_disk(tmp_path: Path) -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v2.2.2'}))
    await latest_release_tag(session, 'owner', 'persist')
    cache_file = tmp_path / 'xdg-cache' / 'wiswa' / 'github_tag_cache.json'
    assert cache_file.is_file()
    on_disk = json.loads(cache_file.read_text(encoding='utf-8'))
    assert on_disk['gh_owner/persist_False_True'] == 'v2.2.2'


@pytest.mark.asyncio
async def test_latest_release_tag_disk_cache_used_when_blocked(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache_dir = tmp_path / 'xdg-cache' / 'wiswa'
    cache_dir.mkdir(parents=True)
    key = 'gh_owner/blocked_False_True'
    (cache_dir / 'github_tag_cache.json').write_text(json.dumps({key: 'v1.9.0'}) + '\n',
                                                     encoding='utf-8')
    session = MagicMock()
    session.get = AsyncMock(side_effect=[
        _make_niquests_response(ok=False, status_code=403),
        _make_niquests_response(ok=False, status_code=403)
    ])
    with caplog.at_level(logging.WARNING):
        assert await latest_release_tag(session, 'owner', 'blocked') == 'v1.9.0'
    assert 'disk-cached' in caplog.text


@pytest.mark.asyncio
async def test_latest_release_tag_blocked_missing_disk_entry_raises(tmp_path: Path) -> None:
    cache_dir = tmp_path / 'xdg-cache' / 'wiswa'
    cache_dir.mkdir(parents=True)
    (cache_dir / 'github_tag_cache.json').write_text('{}\n', encoding='utf-8')
    session = MagicMock()
    session.get = AsyncMock(side_effect=[
        _make_niquests_response(ok=False, status_code=429),
        _make_niquests_response(ok=False, status_code=429)
    ])
    with pytest.raises(ValueError, match='Could not get latest tag'):
        await latest_release_tag(session, 'owner', 'no-disk-entry')


@pytest.mark.asyncio
async def test_latest_release_tag_corrupt_disk_store_overwritten(tmp_path: Path) -> None:
    cache_dir = tmp_path / 'xdg-cache' / 'wiswa'
    cache_dir.mkdir(parents=True)
    (cache_dir / 'github_tag_cache.json').write_text('not json', encoding='utf-8')
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v8.8.8'}))
    assert await latest_release_tag(session, 'owner', 'fresh') == 'v8.8.8'
    data = json.loads((cache_dir / 'github_tag_cache.json').read_text(encoding='utf-8'))
    assert data['gh_owner/fresh_False_True'] == 'v8.8.8'


@pytest.mark.asyncio
async def test_latest_release_tag_disk_write_oserror_logged(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v1.0.1'}))
    real_replace = type(__import__('pathlib').Path()).replace

    def boom_replace(self: Any, target: Any) -> Any:
        if str(self).endswith('.tmp'):
            msg = 'simulated replace failure'
            raise OSError(msg)
        return real_replace(self, target)

    mocker.patch.object(type(__import__('pathlib').Path()), 'replace', boom_replace)
    with caplog.at_level(logging.DEBUG):
        assert await latest_release_tag(session, 'owner', 'write-fail') == 'v1.0.1'
    assert 'persist GitHub tag cache' in caplog.text


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_picks_older_release() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=14)).strftime('%Y-%m-%dT%H:%M:%SZ')
    new_pub = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'v2.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': new_pub
                                       }, {
                                           'tag_name': 'v1.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases])
    result = await latest_release_tag(session, 'owner', 'aged', min_release_age_minutes=10080)
    assert result == 'v1.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_no_match_falls_back(
        caplog: pytest.LogCaptureFixture) -> None:
    new_pub = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'v5.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': new_pub
                                       }])
    latest = _make_niquests_response(ok=True, json_data={'tag_name': 'v5.0.0'})
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases, latest])
    with caplog.at_level(logging.DEBUG):
        result = await latest_release_tag(session, 'owner', 'no-old', min_release_age_minutes=10080)
    assert result == 'v5.0.0'
    assert 'falling back' in caplog.text.lower() or 'predates' in caplog.text


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_blocked_falls_back_to_latest() -> None:
    blocked = _make_niquests_response(ok=False, status_code=403)
    latest = _make_niquests_response(ok=True, json_data={'tag_name': 'v2.0.0'})
    session = MagicMock()
    session.get = AsyncMock(side_effect=[blocked, latest])
    result = await latest_release_tag(session, 'owner', 'age-blocked', min_release_age_minutes=60)
    assert result == 'v2.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_skips_invalid_entries() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    new_pub = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    batch: list[Any] = [
        'not-a-dict',
        {
            'draft': True,
            'tag_name': 'v9.0.0',
            'published_at': old_pub
        },
        {
            'prerelease': True,
            'tag_name': 'v8.0.0',
            'published_at': old_pub
        },
        {
            'tag_name': '',
            'published_at': old_pub
        },
        {
            'tag_name': 'v7.0.0',
            'published_at': 7
        },
        {
            'tag_name': 'v6.0.0',
            'published_at': 'not-a-date'
        },
        {
            'tag_name': 'v5.0.0',
            'published_at': new_pub
        },
        {
            'tag_name': 'vvvv',
            'draft': False,
            'prerelease': False,
            'published_at': old_pub
        },
        {
            'tag_name': 'v2.0.0',
            'draft': False,
            'prerelease': False,
            'published_at': old_pub
        },
    ]
    session = MagicMock()
    session.get = AsyncMock(side_effect=[_make_niquests_response(ok=True, json_data=batch)])
    result = await latest_release_tag(session,
                                      'owner',
                                      'mixed',
                                      min_release_age_minutes=10080,
                                      allow_suffixes=False)
    assert result == 'v2.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_partial_release_page_terminates() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    page = [{'tag_name': 'v1.1.0', 'draft': False, 'prerelease': False, 'published_at': old_pub}]
    session = MagicMock()
    session.get = AsyncMock(side_effect=[_make_niquests_response(ok=True, json_data=page)])
    result = await latest_release_tag(session, 'owner', 'short', min_release_age_minutes=10080)
    assert result == 'v1.1.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_non_list_batch_falls_back() -> None:
    weird = _make_niquests_response(ok=True, json_data={'items': []})
    latest = _make_niquests_response(ok=True, json_data={'tag_name': 'v1.2.3'})
    session = MagicMock()
    session.get = AsyncMock(side_effect=[weird, latest])
    result = await latest_release_tag(session, 'owner', 'non-list', min_release_age_minutes=1)
    assert result == 'v1.2.3'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_http_not_ok_falls_back() -> None:
    nok = _make_niquests_response(ok=False, status_code=404)
    latest = _make_niquests_response(ok=True, json_data={'tag_name': 'v4.0.0'})
    session = MagicMock()
    session.get = AsyncMock(side_effect=[nok, latest])
    result = await latest_release_tag(session, 'owner', 'nok', min_release_age_minutes=5)
    assert result == 'v4.0.0'


@pytest.mark.asyncio
async def test_ref_commit_sha_returns_text() -> None:
    sha = 'a' * 40
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True, text=f'  {sha}\n'))
    assert await ref_commit_sha(session, 'owner', 'repo', 'main') == sha
    session.get.assert_called_once_with('https://api.github.com/repos/owner/repo/commits/main',
                                        headers={'Accept': 'application/vnd.github.sha'},
                                        timeout=15)


@pytest.mark.asyncio
async def test_ref_commit_sha_cache_hit() -> None:
    sha = 'b' * 40
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True, text=sha))
    one = await ref_commit_sha(session, 'owner', 'repo', 'main')
    two = await ref_commit_sha(session, 'owner', 'repo', 'main')
    assert one == two == sha
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_ref_commit_sha_fails_on_not_ok() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=False, status_code=404))
    with pytest.raises(ValueError, match='Could not get commit SHA'):
        await ref_commit_sha(session, 'owner', 'missing', 'main')


@pytest.mark.asyncio
async def test_ref_commit_sha_empty_body_raises() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=True, text='   \n'))
    with pytest.raises(ValueError, match='Could not get commit SHA'):
        await ref_commit_sha(session, 'owner', 'repo', 'main')


@pytest.mark.asyncio
async def test_ref_commit_sha_disk_fallback_on_rate_limit(tmp_path: Path) -> None:
    sha = 'c' * 40
    ok_session = MagicMock()
    ok_session.get = AsyncMock(return_value=_make_niquests_response(ok=True, text=sha))
    assert await ref_commit_sha(ok_session, 'owner', 'repo', 'main') == sha
    clear_tag_cache()
    rate_limited = MagicMock()
    rate_limited.get = AsyncMock(return_value=_make_niquests_response(ok=False, status_code=403))
    assert await ref_commit_sha(rate_limited, 'owner', 'repo', 'main') == sha


@pytest.mark.asyncio
async def test_ref_commit_sha_rate_limited_without_cache_raises() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=_make_niquests_response(ok=False, status_code=429))
    with pytest.raises(ValueError, match='Could not get commit SHA'):
        await ref_commit_sha(session, 'unseen', 'repo', 'main')


def _make_gh_api(mocker: MockerFixture) -> MagicMock:
    api = MagicMock()
    api.patch = AsyncMock()
    api.put = AsyncMock()
    api.post = AsyncMock()
    api.getitem = AsyncMock(return_value={})
    api.getiter = MagicMock(return_value=_aiter([]))
    mocker.patch('wiswa.vcs.github.NiquestsGitHubAPI', return_value=api)
    return api


def _patch_token(mocker: MockerFixture, token: str | None = 'gh-token') -> None:  # noqa: S107
    mocker.patch('wiswa.vcs.github.get_github_token', return_value=token)


@pytest.mark.asyncio
async def test_configure_project_returns_when_no_token(mocker: MockerFixture,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker, token=None)
    new_api = mocker.patch('wiswa.vcs.github.NiquestsGitHubAPI')
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    new_api.assert_not_called()
    assert 'No GitHub token' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_runs_full_pipeline(mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    await configure_project(MagicMock(),
                            repository_uri='https://github.com/owner/repo.git',
                            description='desc',
                            homepage='https://example.com',
                            keywords=['a', 'multi word'],
                            default_branch='main',
                            private=False,
                            immutable_releases=True)
    api.patch.assert_awaited_once()
    assert api.patch.await_args is not None
    assert api.patch.await_args.kwargs['data']['description'] == 'desc'
    topics_call = next(c for c in api.put.await_args_list if c.args and 'topics' in c.args[0])
    assert topics_call.kwargs['data'] == {'names': ['a', 'multi-word']}
    immutable_call = next(
        c for c in api.put.await_args_list if c.args and 'immutable-releases' in c.args[0])
    assert immutable_call is not None
    pages_call = next(c for c in api.post.await_args_list if c.args and 'pages' in c.args[0])
    assert pages_call.kwargs['data'] == {'source': {'branch': 'main', 'path': '/'}}


@pytest.mark.asyncio
async def test_configure_project_skips_pages_when_private(mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    await configure_project(MagicMock(),
                            repository_uri='https://github.com/owner/repo',
                            default_branch='main',
                            private=True)
    assert not any('pages' in (c.args[0] if c.args else '') for c in api.post.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_skips_pages_when_no_default_branch(mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    await configure_project(MagicMock(),
                            repository_uri='https://github.com/owner/repo',
                            default_branch=None)
    assert not any('pages' in (c.args[0] if c.args else '') for c in api.post.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_skips_immutable_when_disabled(mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    await configure_project(MagicMock(),
                            repository_uri='https://github.com/owner/repo',
                            immutable_releases=False)
    assert not any('immutable-releases' in (c.args[0] if c.args else '')
                   for c in api.put.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_logs_patch_failure(mocker: MockerFixture,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.patch = AsyncMock(side_effect=HTTPException(HTTPStatus.BAD_REQUEST, 'bad'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    assert 'repository settings' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_logs_topics_failure(mocker: MockerFixture,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)

    async def fail_topics(url: str, **_kwargs: Any) -> None:  # noqa: RUF029
        if 'topics' in url:
            raise HTTPException(HTTPStatus.FORBIDDEN, 'forbidden')

    api.put = AsyncMock(side_effect=fail_topics)
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(),
                                repository_uri='https://github.com/owner/repo',
                                keywords=['a'])
    assert 'topics' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_logs_security_failures(mocker: MockerFixture,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)

    async def fail_security(url: str, **_kwargs: Any) -> None:  # noqa: RUF029
        if 'automated-security-fixes' in url or 'immutable-releases' in url:
            raise HTTPException(HTTPStatus.FORBIDDEN, 'forbidden')

    api.put = AsyncMock(side_effect=fail_security)
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(),
                                repository_uri='https://github.com/owner/repo',
                                immutable_releases=True)
    assert 'automated-security-fixes' in caplog.text
    assert 'immutable releases' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_sync_rulesets_creates_and_updates(mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.getiter = MagicMock(return_value=_aiter([{
        'name': 'Protect version tags',
        'id': 10
    }, {
        'name': 'Other',
        'id': 11
    }]))
    await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    put_ruleset_calls = [c for c in api.put.await_args_list if c.args and 'rulesets/' in c.args[0]]
    post_ruleset_calls = [c for c in api.post.await_args_list if c.args and 'rulesets' in c.args[0]]
    assert any(c.args[0].endswith('/rulesets/10') for c in put_ruleset_calls)
    post_names = {c.kwargs['data']['name'] for c in post_ruleset_calls}
    assert 'Protect default branch' in post_names
    assert 'Copilot review for default branch' in post_names


@pytest.mark.asyncio
async def test_configure_project_sync_rulesets_listing_failure_short_circuits(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.getiter = MagicMock(side_effect=HTTPException(HTTPStatus.FORBIDDEN, 'forbidden'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    assert 'rulesets' in caplog.text
    assert not any('rulesets' in (c.args[0] if c.args else '') for c in api.post.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_sync_rulesets_apply_failure_logged(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.post = AsyncMock(side_effect=HTTPException(HTTPStatus.FORBIDDEN, 'forbidden'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    assert 'ruleset' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_sync_rulesets_parse_error_logged(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.post = AsyncMock(side_effect=TypeError('string indices must be integers'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    assert 'ruleset' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_bootstrap_pages_skipped_when_already_configured(
        mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.getitem = AsyncMock(return_value={'build_type': 'legacy'})
    await configure_project(MagicMock(),
                            repository_uri='https://github.com/owner/repo',
                            default_branch='main')
    assert not any('/pages' in (c.args[0] if c.args else '') for c in api.post.await_args_list)


@pytest.mark.asyncio
async def test_configure_project_bootstrap_pages_failure_logged(
        mocker: MockerFixture, caplog: pytest.LogCaptureFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.post = AsyncMock(side_effect=HTTPException(HTTPStatus.FORBIDDEN, 'forbidden'))
    with caplog.at_level(logging.WARNING):
        await configure_project(MagicMock(),
                                repository_uri='https://github.com/owner/repo',
                                default_branch='main')
    assert 'Pages' in caplog.text


@pytest.mark.asyncio
async def test_configure_project_sync_rulesets_ignores_malformed_entries(
        mocker: MockerFixture) -> None:
    _patch_token(mocker)
    api = _make_gh_api(mocker)
    api.getiter = MagicMock(return_value=_aiter([
        'not-a-dict',
        {
            'name': 7,
            'id': 1
        },
        {
            'name': 'Protect version tags',
            'id': 'not-int'
        },
        {
            'name': 'Protect version tags',
            'id': 42
        },
    ]))
    await configure_project(MagicMock(), repository_uri='https://github.com/owner/repo')
    put_ruleset_calls = [
        c for c in api.put.await_args_list if c.args and c.args[0].endswith('/rulesets/42')
    ]
    assert put_ruleset_calls


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_with_invalid_semver_skipped() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'v!!!',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }, {
                                           'tag_name': 'v2.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases])
    assert await latest_release_tag(session,
                                    'owner',
                                    'invalid-semver',
                                    min_release_age_minutes=10080,
                                    allow_suffixes=True) == 'v2.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_require_v_prefix_rejects_non_v_tag() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'release-0.40',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }, {
                                           'tag_name': 'v0.40.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases])
    assert await latest_release_tag(session,
                                    'google',
                                    'yapf',
                                    min_release_age_minutes=10080,
                                    allow_suffixes=True,
                                    require_v_prefix=True) == 'v0.40.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_no_digit_suffix_when_disabled() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'v0.40.0-beta',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }, {
                                           'tag_name': 'v0.40.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases])
    assert await latest_release_tag(session,
                                    'google',
                                    'yapf',
                                    min_release_age_minutes=10080,
                                    allow_suffixes=False) == 'v0.40.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_exhausts_full_page(mocker: MockerFixture) -> None:
    mocker.patch('wiswa.vcs.github._GITHUB_RELEASES_PAGE_CAP', 1)
    new_pub = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    page1 = [{
        'tag_name': f'v50.{i}.0',
        'draft': False,
        'prerelease': False,
        'published_at': new_pub
    } for i in range(100)]
    latest = _make_niquests_response(ok=True, json_data={'tag_name': 'v50.0.0'})
    session = MagicMock()
    session.get = AsyncMock(side_effect=[_make_niquests_response(ok=True, json_data=page1), latest])
    assert await latest_release_tag(session, 'owner', 'full-page',
                                    min_release_age_minutes=10080) == 'v50.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_age_gate_keeps_highest_eligible_version() -> None:
    old_pub = (datetime.now(tz=timezone.utc) - timedelta(days=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    releases = _make_niquests_response(ok=True,
                                       json_data=[{
                                           'tag_name': 'v3.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }, {
                                           'tag_name': 'v2.0.0',
                                           'draft': False,
                                           'prerelease': False,
                                           'published_at': old_pub
                                       }])
    session = MagicMock()
    session.get = AsyncMock(side_effect=[releases])
    assert await latest_release_tag(session,
                                    'owner',
                                    'higher',
                                    min_release_age_minutes=10080,
                                    allow_suffixes=False) == 'v3.0.0'


@pytest.mark.asyncio
async def test_latest_release_tag_reuses_disk_store_memo_across_calls(tmp_path: Path) -> None:
    session = MagicMock()
    session.get = AsyncMock(
        return_value=_make_niquests_response(ok=True, json_data={'tag_name': 'v1.0.0'}))
    await latest_release_tag(session, 'owner', 'first')
    await latest_release_tag(session, 'owner', 'second')
    cache_file = tmp_path / 'xdg-cache' / 'wiswa' / 'github_tag_cache.json'
    on_disk = json.loads(cache_file.read_text(encoding='utf-8'))
    assert on_disk == {
        'gh_owner/first_False_True': 'v1.0.0',
        'gh_owner/second_False_True': 'v1.0.0',
    }
