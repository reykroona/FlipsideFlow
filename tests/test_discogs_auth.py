import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from urllib3.exceptions import ProtocolError

from backend import api
from discogs_auth import DiscogsAuthError, validate_token


def response(code=200, body=None):
    return SimpleNamespace(status_code=code, json=lambda: {"username": "listener"} if body is None else body)


def test_identity_uses_auth_header_timeout_and_no_redirects(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response()
    monkeypatch.setattr('discogs_auth.requests.get', get)
    assert validate_token(' private-token ', 'FlipsideFlow/test') == 'listener'
    url, options = calls[0]
    assert url == 'https://api.discogs.com/oauth/identity'
    assert 'private-token' not in url
    assert options['headers']['Authorization'] == 'Discogs token=private-token'
    assert options['timeout'] == (5, 10)
    assert options['allow_redirects'] is False


@pytest.mark.parametrize('status,expected', [(401,400),(403,403),(429,429),(500,502),(302,502)])
def test_provider_statuses_have_safe_distinct_errors(monkeypatch, status, expected):
    monkeypatch.setattr('discogs_auth.requests.get', lambda *a, **kw: response(status, {'message': 'private-token'}))
    with pytest.raises(DiscogsAuthError) as error:
        validate_token('private-token')
    assert error.value.status_code == expected
    assert 'private-token' not in str(error.value)


@pytest.mark.parametrize('error,expected,phrase', [
    (requests.ConnectionError(ProtocolError('Connection aborted.', FileNotFoundError(2, 'No such file or directory'))),503,'missing a local HTTPS support file'),
    (FileNotFoundError('private-token'),503,'missing a local HTTPS support file'),
    (requests.exceptions.SSLError('private-token'),503,'secure connection'),
    (requests.Timeout('private-token'),504,'timed out'),
    (requests.ConnectionError('private-token'),503,'Could not connect'),
])
def test_network_errors_do_not_blame_or_reveal_token(monkeypatch, error, expected, phrase):
    def get(*args, **kwargs):
        raise error
    monkeypatch.setattr('discogs_auth.requests.get', get)
    with pytest.raises(DiscogsAuthError) as caught:
        validate_token('private-token')
    assert caught.value.status_code == expected
    assert phrase in str(caught.value)
    assert 'private-token' not in str(caught.value)


@pytest.mark.parametrize('body', [{}, [], {'username': ''}, {'username': 1}])
def test_malformed_identity_is_not_accepted(monkeypatch, body):
    monkeypatch.setattr('discogs_auth.requests.get', lambda *a, **kw: response(body=body))
    with pytest.raises(DiscogsAuthError) as caught:
        validate_token('private-token')
    assert caught.value.status_code == 502


def test_setup_retains_existing_credentials_on_connection_failure(monkeypatch):
    saves = []
    monkeypatch.setattr(api.config, 'save_token', lambda *args: saves.append(args))
    def get(*args, **kwargs):
        raise requests.ConnectionError(ProtocolError('Connection aborted.', FileNotFoundError(2, 'No such file or directory')))
    monkeypatch.setattr('discogs_auth.requests.get', get)
    reply = TestClient(api.app).post('/api/setup/discogs-token', json={'token': 'private-token'})
    assert reply.status_code == 503
    assert 'app error, not an invalid token' in reply.json()['detail']
    assert 'private-token' not in reply.text
    assert saves == []


def test_setup_success_saves_and_reinitializes_without_network_on_event_loop(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    def validate(*args):
        entered.set()
        assert finish.wait(3)
        return 'listener'
    monkeypatch.setattr('discogs_auth.validate_token', validate)
    saves, reloads = [], []
    monkeypatch.setattr(api.config, 'save_token', lambda *a: saves.append(a) or True)
    monkeypatch.setattr(api.config, 'reload', lambda: reloads.append(True))
    monkeypatch.setattr(api.metadata_handler, 'reinitialize', lambda *a: None)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://testserver') as client:
            pending = asyncio.create_task(client.post('/api/setup/discogs-token', json={'token': ' private-token '}))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(0.01)
                assert entered.is_set()
                assert not pending.done(), 'validation must run on a worker, not stall this event loop'
                assert (await client.get('/api/recording/status')).status_code == 200
            finally:
                finish.set()
            reply = await pending
            assert reply.status_code == 200 and reply.json()['username'] == 'listener'
            assert 'private-token' not in reply.text
    asyncio.run(exercise())
    assert saves == [('private-token', 'FlipsideFlow/1.0')] and reloads == [True]
