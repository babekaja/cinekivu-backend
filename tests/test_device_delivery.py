from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cinekivu_source import api as cine, delivery
from cinekivu_source.device_delivery import candidates, allowed_device_url, resolve_download
from moviebox_api_server import app


def source(**extra):
    return {'url': 'https://bcdnxw.hakunaymatata.com/video.mp4?sign=opaque&t=123',
            'format': 'MP4', 'quality_value': 480, 'duration_sec': 3600, **extra}


@pytest.mark.parametrize('url', [
    'http://bcdnxw.hakunaymatata.com/video.mp4', 'https://127.0.0.1/video.mp4',
    'https://bcdnxw.hakunaymatata.com.evil.test/video.mp4',
    'https://user:pass@bcdnxw.hakunaymatata.com/video.mp4',
    'https://bcdnxw.hakunaymatata.com:444/video.mp4',
    'https://bcdnxw.hakunaymatata.com/notice.mp4',
    'https://bcdnxw.hakunaymatata.com/master.mpd',
    'https://bcdnxw.hakunaymatata.com/video.mp4#fragment',
])
def test_device_rejects_unsafe_non_mp4_and_notice_urls(url):
    assert not allowed_device_url(url)


def test_device_candidates_never_inherit_auth_cookies_or_forwarded_ips():
    assert candidates([source(cookie='SECRET')], '123', 1, 2) == []
    assert candidates([source(headers={'authorization': 'SECRET'})], '123', 1, 2) == []
    item = candidates([source(headers={'X-Real-IP': '8.8.8.8', 'x-client-token': 'SECRET'})], '123', 1, 2)[0]
    assert item['validation'] == 'required_on_device'
    assert 'SECRET' not in str(item) and '8.8.8.8' not in str(item)
    assert item['id'] == 'md-123-s1e2-q480'
    assert item['download']['resume_url'] == '/api/download/device/md-123-s1e2-q480'
    assert 'play_url' not in item


def test_device_filters_short_notices_and_deduplicates_quality():
    items = candidates([source(), source(), source(quality_value=720, duration_sec=20)], '123', 1, 2)
    assert len(items) == 1 and items[0]['quality_value'] == 480


def test_device_route_never_probes_cdn_or_claims_server_playability(monkeypatch):
    calls = []
    monkeypatch.setattr(cine.engine, 'get_streams', lambda sid, **kw: calls.append(kw) or [source()])
    monkeypatch.setattr(delivery, 'prepare_streams', lambda *a: pytest.fail('No server CDN probe in device mode'))
    response = TestClient(app).get('/api/v1/cinekivu/titles/123/streams', params={
        'subject_type': 2, 'detail_path': 'series', 'season': 2, 'episode': 3, 'delivery_mode': 'device',
        'probe_transport': 'true'})
    data = response.json()['data']
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert data['sources'] == [] and data['playable_count'] == 0 and data['candidate_count'] == 1
    assert data['device_candidates'][0]['id'] == 'md-123-s2e3-q480'
    assert calls == [{'season': 2, 'episode': 3, 'detail_path': 'series', 'device_delivery': True}]


def test_device_movie_identity_and_empty_response(monkeypatch):
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **kw: [source()])
    client = TestClient(app)
    data = client.get('/api/v1/cinekivu/titles/123/streams?subject_type=1&detail_path=film&delivery_mode=device').json()['data']
    assert data['device_candidates'][0]['id'] == 'md-123-s0e0-q480'
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **kw: [])
    data = client.get('/api/v1/cinekivu/titles/123/streams?subject_type=1&detail_path=film&delivery_mode=device').json()['data']
    assert data['candidate_count'] == 0 and data['message']


def test_native_mode_skips_server_probe_and_mobile_fallback(monkeypatch):
    monkeypatch.setattr(cine, 'WEB_DOMAINS', ['https://web.example.test'])
    engine = object.__new__(cine.MovieBoxEngine)
    engine.bootstrap_token = lambda: 'visitor'
    engine.session = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(status_code=200,
        json=lambda: {'data': {'streams': [dict(source(), resolutions='480')]}}))
    engine.native_play_info = lambda *a: pytest.fail('Unnecessary mobile fallback')
    monkeypatch.setattr(cine, '_probe_mp4_playback', lambda *a: pytest.fail('Server media probe'))
    result = engine.get_streams('123', season=1, episode=1, detail_path='series', device_delivery=True)
    assert len(result) == 1


def test_download_resolves_same_episode_and_quality_only(monkeypatch):
    seen = []
    monkeypatch.setattr(cine.engine, 'get_streams', lambda sid, **kw: seen.append((sid, kw)) or [source()])
    response = TestClient(app).get('/api/download/device/md-123-s1e2-q480')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json()['data']['id'] == 'md-123-s1e2-q480'
    assert seen == [('123', {'season': 1, 'episode': 2, 'device_delivery': True})]
    assert TestClient(app).get('/api/download/device/md-123-s1e2-q720').status_code == 409
    assert TestClient(app).get('/api/download/device/invalid').status_code == 422
