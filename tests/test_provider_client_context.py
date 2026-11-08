import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cinekivu_client_context import (ProviderClientContextMiddleware, provider_client_headers,
                                     provider_client_ip, public_ip, _client_ip)
from cinekivu_source import api as cine, delivery
from starlette.requests import Request


def request(peer, headers=None):
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers':
        [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        'client': (peer, 1234), 'server': ('example.org', 443), 'scheme': 'https', 'query_string': b''})


@pytest.mark.parametrize('value', ['127.0.0.1', '10.0.0.1', '192.168.1.1', '169.254.169.254',
                                  '::1', '8.8.8.8, 1.1.1.1', '8.8.8.8\r\nCookie: secret', 'invalid'])
def test_private_malformed_or_multiple_addresses_are_not_forwarded(value):
    assert public_ip(value) is None


def test_non_render_never_trusts_caller_forwarded_headers(monkeypatch):
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setenv('CINEKIVU_FORWARD_CLIENT_IP', '1')
    assert provider_client_ip(request('8.8.8.8', {'X-Forwarded-For': '1.1.1.1', 'CF-Connecting-IP': '1.1.1.1'})) == '8.8.8.8'
    assert provider_client_ip(request('127.0.0.1', {'X-Forwarded-For': '8.8.8.8'})) is None


def test_render_uses_only_cloudflare_platform_header_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.delenv('CINEKIVU_FORWARD_CLIENT_IP', raising=False)
    assert provider_client_ip(request('10.0.0.1', {'CF-Connecting-IP': '8.8.8.8', 'X-Forwarded-For': '1.1.1.1'})) == '8.8.8.8'
    assert provider_client_ip(request('10.0.0.1', {'X-Forwarded-For': '8.8.8.8'})) is None
    monkeypatch.setenv('CINEKIVU_FORWARD_CLIENT_IP', '0')
    assert provider_client_ip(request('8.8.8.8', {'CF-Connecting-IP': '8.8.8.8'})) is None


def test_context_is_copied_to_worker_threads_and_isolated_between_clients(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    monkeypatch.delenv('CINEKIVU_FORWARD_CLIENT_IP', raising=False)
    app = FastAPI()
    app.add_middleware(ProviderClientContextMiddleware)
    @app.get('/probe')
    async def probe():
        await asyncio.sleep(.01)
        return await asyncio.to_thread(provider_client_headers)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://example.test') as client:
            responses = await asyncio.gather(*[client.get('/probe', headers={'CF-Connecting-IP': ip})
                                              for ip in ['8.8.8.8', '1.1.1.1']])
            assert [r.json()['X-Real-IP'] for r in responses] == ['8.8.8.8', '1.1.1.1']
    asyncio.run(exercise())
    assert provider_client_headers() == {}


def test_web_request_and_media_headers_receive_only_validated_context(monkeypatch):
    monkeypatch.setattr(cine, 'WEB_DOMAINS', ['https://web.example.test'])
    engine = object.__new__(cine.MovieBoxEngine)
    engine.bootstrap_token = lambda: 'visitor'
    calls = []
    def get(url, **kw):
        calls.append(kw['headers'])
        return SimpleNamespace(status_code=200, json=lambda: {'code':0,'data':{'streams':[{
            'url':'https://cdn.example.test/video.mp4','format':'MP4','resolutions':'720','duration':3600}]}})
    engine.session = SimpleNamespace(get=get)
    engine._resource_streams = lambda *args: ([], None)
    monkeypatch.setattr(cine, '_probe_mp4_playback', lambda *args: None)
    token = _client_ip.set('8.8.8.8')
    try:
        streams = engine.get_streams('123', detail_path='series', prefer_web_mp4=True)
    finally:
        _client_ip.reset(token)
    assert calls[0]['X-Real-IP'] == '8.8.8.8'
    assert calls[0]['Sec-Fetch-Site'] == 'same-origin'
    assert delivery._media_headers(streams[0])['X-Forwarded-For'] == '8.8.8.8'
    assert 'X-Real-IP' not in delivery._media_headers({'headers':{'X-Real-IP':'127.0.0.1'}})


def test_web_expired_guest_token_refreshes_once(monkeypatch):
    monkeypatch.setattr(cine, 'WEB_DOMAINS', ['https://web.example.test'])
    engine = object.__new__(cine.MovieBoxEngine)
    renewals = []
    engine.bootstrap_token = lambda **kw: renewals.append(kw.get('force', False)) or 'visitor'
    engine.session = SimpleNamespace(get=lambda *args, **kw: SimpleNamespace(status_code=200,
        json=lambda: {'code':441,'data':{}}))
    engine.native_play_info = lambda *args: {}
    engine._resource_streams = lambda *args: ([], None)
    assert engine.get_streams('123', detail_path='series', prefer_web_mp4=True) == []
    assert renewals == [False, True]
