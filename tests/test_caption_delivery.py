"""Exercise the real caption fetch/conversion path against a deterministic HTTP origin."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import cinekivu_bridge as bridge
from moviebox_api_server import app


@pytest.fixture
def caption_origin(monkeypatch):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(bridge.delivery, '_public_https_media_url',
                        lambda url: url.startswith('https://captions.example.org/'))
    bridge._caption_sources['caption-audit'] = {
        'url': 'https://captions.example.org/fr.srt?private=not-for-clients',
        'expires': time.monotonic() + 60}
    def install(handler):
        monkeypatch.setattr(bridge.httpx, 'AsyncClient', lambda **kwargs:
                            original_client(transport=httpx.MockTransport(handler), **kwargs))
        return TestClient(app)
    yield install
    bridge._caption_sources.pop('caption-audit', None)


@pytest.mark.parametrize('status,expected', [(404,404), (410,404), (403,502), (503,502)])
def test_missing_cdn_file_is_distinct_from_upstream_server_failure(caption_origin, status, expected):
    client = caption_origin(lambda request: httpx.Response(status))
    response = client.get('/api/v1/cinekivu/captions/caption-audit.vtt')
    assert response.status_code == expected
    assert 'captions.example.org' not in response.text and 'private=' not in response.text
    if expected == 404:
        assert response.json()['detail'] == 'Fichier de sous-titres absent chez le fournisseur'


def test_real_caption_fetch_converts_srt_without_exposing_source(caption_origin):
    client = caption_origin(lambda request: httpx.Response(200,
        content=b'1\n00:00:01,000 --> 00:00:02,000\nBonjour\n'))
    response = client.get('/api/v1/cinekivu/captions/caption-audit.vtt')
    assert response.status_code == 200
    assert response.content.startswith(b'WEBVTT\n')
    assert b'00:00:01.000 --> 00:00:02.000' in response.content
    assert response.headers['content-type'].startswith('text/vtt')
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_caption_does_not_follow_untrusted_redirect(caption_origin):
    calls = []
    def origin(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/private'})
    client = caption_origin(origin)
    assert client.get('/api/v1/cinekivu/captions/caption-audit.vtt').status_code == 502
    assert calls == ['captions.example.org']


@pytest.mark.parametrize('declared', [True, False])
def test_caption_fetch_enforces_size_with_or_without_length(caption_origin, monkeypatch, declared):
    monkeypatch.setattr(bridge, 'MAX_CAPTION', 32)
    headers = {'Content-Length': '100'} if declared else {}
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 20
            yield b'y' * 20
    client = caption_origin(lambda request: httpx.Response(200, headers=headers, stream=Body()))
    assert client.get('/api/v1/cinekivu/captions/caption-audit.vtt').status_code == 413


def test_caption_expiration_does_not_fetch_origin(caption_origin):
    def unexpected(request):
        raise AssertionError('An expired token must not reach the CDN')
    client = caption_origin(unexpected)
    bridge._caption_sources['caption-audit']['expires'] = time.monotonic()-1
    response = client.get('/api/v1/cinekivu/captions/caption-audit.vtt')
    assert response.status_code == 404
    assert 'expirés' in response.json()['detail']


@pytest.mark.parametrize('failure', [False, True])
def test_film_captions_survive_missing_detail_metadata(monkeypatch, failure):
    from cinekivu_source import api as cine
    def details(sid):
        if failure:
            raise RuntimeError('Temporary metadata outage')
        return {'subject_id': sid, 'subject_type': None}
    calls = []
    def captions(sid, season, episode):
        calls.append((season, episode))
        return [{'id': 'en', 'code': 'en', 'url': 'https://captions.example.org/en.srt'}] if (season, episode) == (0, 0) else []
    monkeypatch.setattr(cine.engine, 'get_details', details)
    monkeypatch.setattr(cine.engine, 'get_subtitles', captions)
    monkeypatch.setattr(bridge.delivery, '_public_https_media_url', lambda url: True)
    response = TestClient(app).get('/api/v1/cinekivu/titles/movie/subtitles')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['items'][0]['lang'] == 'en' and (data['season'], data['episode']) == (0, 0)
    assert calls == [(1, 1), (0, 0)]
