from types import SimpleNamespace

import network_checks


def test_offline_check_creates_a_verified_tls_context_without_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('offline packaging check must not use the network')
    monkeypatch.setattr(network_checks.requests, 'get', no_network)
    result = network_checks.check_https()
    assert result['tls'] == 'ok' and result['trusted_ca_count'] > 0


def test_online_check_uses_public_endpoints_without_tokens(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(network_checks.requests, 'get', get)
    result = network_checks.check_https(online=True)
    assert result['discogs'] == result['audd'] == 200
    assert {url for url, _ in calls} == {'https://api.discogs.com/', 'https://api.audd.io/'}
    assert all('Authorization' not in kw['headers'] and kw['allow_redirects'] is False for _, kw in calls)
