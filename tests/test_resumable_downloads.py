"""Resume URLs survive token expiry and process registry loss without losing Range validators."""
import httpx
from fastapi.testclient import TestClient
from cinekivu_source import api as cine, delivery
from moviebox_api_server import app

client = TestClient(app)


def test_stable_link_re_resolves_after_registry_loss_and_preserves_if_range(monkeypatch):
    body = b'\x00\x00\x00\x20ftypisom' + b'x' * 100
    metadata = {'format': 'MP4', 'url': 'https://cdn.example.org/video.mp4',
                'quality': '480p', 'quality_value': 480, 'source': 'web', 'codec': 'h264'}
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda *a: True)
    monkeypatch.setattr(delivery, '_probe_mp4_playback', lambda *a: None)
    entry = delivery.prepare_streams([metadata.copy()])[0]
    download = delivery.register_download(entry, '123', 2, 3)
    endpoint = download['resume_url']
    seen, resolutions = [], []
    def resolve(sid, **params):
        resolutions.append((sid, params['season'], params['episode']))
        return [metadata.copy()]
    monkeypatch.setattr(cine.engine, 'get_streams', resolve)
    def cdn(request):
        seen.append(request)
        headers = {'Content-Type': 'video/mp4', 'ETag': '"same-video"',
                   'Last-Modified': 'Wed, 28 Oct 2026 00:00:00 GMT', 'Accept-Ranges': 'bytes'}
        if request.headers.get('range') == 'bytes=32-' and request.headers.get('if-range') == '"same-video"':
            return httpx.Response(206, stream=httpx.ByteStream(body[32:]), headers={**headers,
                'Content-Length': str(len(body)-32), 'Content-Range': f'bytes 32-{len(body)-1}/{len(body)}'})
        return httpx.Response(200, stream=httpx.ByteStream(body), headers={**headers, 'Content-Length': str(len(body))})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(cdn), **kw))
    # A restarted backend has no old playback or download token registry.
    delivery._playback_sources.clear()
    delivery._download_sources.clear()
    response = client.get(endpoint, headers={'Range': 'bytes=32-', 'If-Range': '"same-video"'})
    assert response.status_code == 206 and response.content == body[32:]
    assert response.headers['etag'] == '"same-video"'
    assert response.headers['last-modified'] == 'Wed, 28 Oct 2026 00:00:00 GMT'
    assert resolutions == [('123', 2, 3)]
    assert seen[-1].headers['if-range'] == '"same-video"'
    changed = client.get(endpoint, headers={'Range': 'bytes=32-', 'If-Range': '"old-version"'})
    assert changed.status_code == 200 and changed.content == body
    assert client.get('/api/download/by-id/not-a-media-id').status_code == 422


def test_expired_cdn_signature_is_renewed_on_resume(monkeypatch):
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda *a: True)
    monkeypatch.setattr(delivery, '_probe_mp4_playback', lambda *a: None)
    item = {'format': 'MP4', 'url': 'https://cdn.example.org/old.mp4', 'quality': '720p', 'quality_value': 720}
    old = delivery.prepare_streams([item.copy()])[0]
    endpoint = delivery.register_download(old, '456', 0, 0)['resume_url']
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **kw: [{**item, 'url': 'https://cdn.example.org/new.mp4'}])
    seen = []
    def cdn(request):
        seen.append(request.url.path)
        if request.url.path == '/old.mp4': return httpx.Response(403)
        return httpx.Response(200, stream=httpx.ByteStream(b'full video'), headers={'Content-Type':'video/mp4','Content-Length':'10'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(cdn), **kw))
    assert client.get(endpoint).content == b'full video'
    assert seen == ['/old.mp4', '/new.mp4']
