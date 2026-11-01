"""Synthetic transport checks; never contact a real payment or provider service."""
import importlib
import socket

import httpx
import pytest

from test_flows import env as env


def addresses(*values, port=443):
    return [(socket.AF_INET6 if ':' in value else socket.AF_INET,
             socket.SOCK_STREAM, 6, '', (value, port)) for value in values]


@pytest.mark.parametrize('address', ['1.1.1.1', '2606:4700:4700::1111'])
def test_dispatch_uses_checked_ip_with_original_host_and_tls_name(env, monkeypatch, address):
    import worker
    worker = importlib.reload(worker)
    resolutions = []

    def resolve(host, port, **kwargs):
        resolutions.append((host, port))
        return addresses(address, port=port)

    monkeypatch.setattr(worker.socket, 'getaddrinfo', resolve)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'reference': 'synthetic-order', 'status': 'processing'})

    original_client = httpx.Client
    monkeypatch.setattr(worker.httpx, 'Client',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    answer = worker.provider_request('POST', 'https://provider.example:8443/orders',
                                     'synthetic-key', {'reference': 'synthetic-order'})
    assert answer['status'] == 'processing'
    assert resolutions == [('provider.example', 8443)]
    assert requests[0].url.host == address
    assert requests[0].url.port == 8443
    assert requests[0].headers['host'] == 'provider.example:8443'
    assert requests[0].extensions['sni_hostname'] == 'provider.example'


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_failure_tries_next_checked_address(env, monkeypatch, error):
    import worker
    monkeypatch.setattr(worker.socket, 'getaddrinfo',
                        lambda *a, **k: addresses('2606:4700:4700::1111', '1.1.1.1'))
    attempts = []

    def handle(request):
        attempts.append(request.url.host)
        if len(attempts) == 1:
            raise error('synthetic connection failure', request=request)
        return httpx.Response(200, json={'status': 'processing'})

    original_client = httpx.Client
    monkeypatch.setattr(worker.httpx, 'Client',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    assert worker.provider_request('POST', 'https://provider.example/orders',
                                   'synthetic-key')['status'] == 'processing'
    assert attempts == ['2606:4700:4700::1111', '1.1.1.1']


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.WriteError])
def test_ambiguous_post_is_never_replayed_on_another_address(env, monkeypatch, error):
    import worker
    monkeypatch.setattr(worker.socket, 'getaddrinfo',
                        lambda *a, **k: addresses('1.1.1.1', '8.8.8.8'))
    attempts = []

    def handle(request):
        attempts.append(request.url.host)
        raise error('synthetic ambiguous outcome', request=request)

    original_client = httpx.Client
    monkeypatch.setattr(worker.httpx, 'Client',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(error):
        worker.provider_request('POST', 'https://provider.example/orders', 'synthetic-key')
    assert attempts == ['1.1.1.1']


@pytest.mark.parametrize('values', [[], ['127.0.0.1'], ['169.254.169.254'],
                                  ['1.1.1.1', '10.0.0.1'], ['224.0.0.1']])
def test_unsafe_dns_answers_are_rejected(env, monkeypatch, values):
    import worker
    monkeypatch.setattr(worker.socket, 'getaddrinfo', lambda *a, **k: addresses(*values))
    with pytest.raises(ValueError):
        worker.public_endpoint('https://provider.example/orders')


@pytest.mark.parametrize('url', ['http://provider.example/orders',
                               'https://user:password@provider.example/orders',
                               'https://provider.example/orders#fragment'])
def test_invalid_provider_urls_are_rejected_before_dns(env, monkeypatch, url):
    import worker
    def unexpected_lookup(*args, **kwargs):
        pytest.fail('Invalid destination must not be resolved')
    monkeypatch.setattr(worker.socket, 'getaddrinfo', unexpected_lookup)
    with pytest.raises(ValueError):
        worker.public_endpoint(url)
