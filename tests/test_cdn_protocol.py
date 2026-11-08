"""Keep protocol refusals useful without publishing origin headers or URLs."""
import httpx
import pytest

from cinekivu_source.delivery import _cdn_refusal


@pytest.mark.parametrize('protocol', [b'HTTP/1.1', b'HTTP/2'])
def test_upgrade_refusal_reports_negotiated_protocol(protocol):
    response = httpx.Response(426, headers={'Upgrade': 'h2'},
                              extensions={'http_version': protocol})
    issue = _cdn_refusal(response, 'cette source vidéo')
    assert protocol.decode() in issue
    assert 'protocole requis h2' in issue


def test_upgrade_refusal_omits_arbitrary_origin_content():
    response = httpx.Response(426, headers={'Upgrade': 'https://origin.example/private?token=secret'})
    issue = _cdn_refusal(response, 'cette source vidéo')
    assert 'HTTP 426' in issue
    assert 'secret' not in issue and 'origin.example' not in issue


@pytest.mark.parametrize('status', [426, 428])
def test_cdn_profile_fallback_is_verified_and_persisted_for_relay(monkeypatch, status):
    from cinekivu_source import delivery
    calls = []
    original = httpx.Client
    class Sample(httpx.SyncByteStream):
        def __iter__(self):
            yield b'\x00\x00\x00\x20ftyp' + b'\x00' * 24
    def origin(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status)
        assert request.headers['Range'] == 'bytes=0-31'
        assert 'Origin' not in request.headers and 'Referer' not in request.headers
        assert 'X-Forwarded-For' not in request.headers
        assert request.headers['User-Agent'].startswith('com.community.oneroom/')
        return httpx.Response(206, headers={'Content-Type': 'video/mp4',
                              'Content-Range': 'bytes 0-31/1234567'}, stream=Sample())
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda url: True)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(origin), **kw))
    stream = {'url': 'https://bcdn.hakunaymatata.com/video.mp4?sign=private',
              'headers': {'User-Agent': 'Mozilla/5.0', 'Origin': 'https://mzfi.me',
                          'Referer': 'https://mzfi.me/', 'X-Forwarded-For': '8.8.8.8'}}
    assert delivery._probe_mp4_playback(stream, 3000) is None
    replay = delivery._media_headers(stream)
    assert replay['User-Agent'].startswith('com.community.oneroom/')
    assert 'Origin' not in replay and 'X-Forwarded-For' not in replay
    assert 'Range' not in replay
    assert len(calls) == 2 and calls[0].url == calls[1].url


@pytest.mark.parametrize('host,status', [
    ('bcdn.hakunaymatata.com', 403), ('bcdn.hakunaymatata.com', 404),
    ('hakunaymatata.com.attacker.example', 426), ('unrelated.example', 428)])
def test_profile_fallback_does_not_retry_auth_errors_or_other_hosts(host, status):
    from cinekivu_source.delivery import _cdn_stream
    calls = []
    def origin(request):
        calls.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(origin)) as client:
        with _cdn_stream(client, f'https://{host}/video.mp4', {'Range': 'bytes=0-31'}) as (response, headers):
            assert response.status_code == status
    assert len(calls) == 1


def test_failed_android_fallback_does_not_mark_source_playable(monkeypatch):
    from cinekivu_source import delivery
    original = httpx.Client
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda url: True)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(426)), **kw))
    stream = {'url': 'https://bcdn.hakunaymatata.com/video.mp4', 'headers': {'User-Agent': 'Mozilla/5.0'}}
    assert 'HTTP 426' in delivery._probe_mp4_playback(stream, 3000)
    assert stream['headers']['User-Agent'] == 'Mozilla/5.0'
