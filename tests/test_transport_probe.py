import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cinekivu_source import transport_probe as probe
from cinekivu_source import api as cine
from moviebox_api_server import app


@pytest.mark.parametrize('streams', [[], [{'format': 'DASH', 'url': 'https://cdn.test/a.mpd'}]])
def test_no_candidate_does_not_issue_network_requests(streams, monkeypatch):
    monkeypatch.setattr(probe, '_httpx_probe', lambda *a: pytest.fail('unexpected fetch'))
    assert probe.compare_transports(streams)['available'] is False


def test_private_candidate_is_rejected_before_any_transport(monkeypatch):
    monkeypatch.setattr(probe, '_httpx_probe', lambda *a: pytest.fail('unexpected fetch'))
    result = probe.compare_transports([{'format': 'MP4', 'url': 'http://127.0.0.1/private'}])
    assert result == {'available': False, 'reason': 'invalid_media_candidate'}


def test_both_clients_receive_same_explicit_headers_without_private_source_fields(monkeypatch):
    monkeypatch.setattr(probe, '_public_https_media_url', lambda url: True)
    seen = []
    def request(url, headers):
        seen.append((url, headers.copy()))
        return {'http_status': 426, 'mp4_verified': False}
    monkeypatch.setattr(probe, '_httpx_probe', request)
    monkeypatch.setattr(probe, '_curl_probe', request)
    result = probe.compare_transports([{'format': 'MP4', 'url': 'https://cdn.test/a.mp4?sign=secret',
        'headers': {'Authorization': 'private-token'}, 'cookie': 'private-cookie'}])
    assert seen[0] == seen[1]
    assert seen[0][1]['Range'] == 'bytes=0-31'
    assert not any(k in seen[0][1] for k in ['Authorization', 'Cookie', 'X-Forwarded-For'])
    assert 'secret' not in json.dumps(result) and 'private-' not in json.dumps(result)


@pytest.mark.parametrize('status', [206, 429])
def test_curl_probe_reads_only_the_requested_range_and_closes(monkeypatch, status):
    curl = pytest.importorskip('curl_cffi')
    instances = []
    class Handle:
        def __init__(self):
            self.options = {}; self.closed = False; instances.append(self)
        def impersonate(self, name, default_headers):
            assert name.startswith('chrome') and default_headers is False
            return 0
        def setopt(self, name, value): self.options[name] = value
        def perform(self):
            header = self.options[curl.CurlOpt.HEADERFUNCTION]
            header(f'HTTP/2 {status}\r\n'.encode())
            header(b'Content-Type: video/mp4\r\n')
            if status == 206: header(b'Content-Range: bytes 0-31/123456\r\n')
            writer = self.options[curl.CurlOpt.WRITEFUNCTION]
            count = writer(b'\x00\x00\x00\x20ftyp' + b'\x00' * 24)
            if status == 429:
                assert count == 0
                raise RuntimeError('Expected deliberate abort with private-url')
        def getinfo(self, key): return status if key == curl.CurlInfo.RESPONSE_CODE else 3
        def close(self): self.closed = True
    monkeypatch.setattr(curl, 'Curl', Handle)
    result = probe._curl_probe('https://cdn.test/a.mp4?sign=secret', {'Range': 'bytes=0-31'})
    assert result['http_status'] == status
    assert result['mp4_verified'] is (status == 206)
    assert result['sample_bytes'] == (32 if status == 206 else 0)
    assert instances[0].closed
    assert instances[0].options[curl.CurlOpt.FOLLOWLOCATION] == 0
    assert 'secret' not in json.dumps(result) and 'private-url' not in json.dumps(result)


def test_comparison_does_not_register_or_authorize_playback(monkeypatch):
    import cinekivu_bridge as bridge
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **k: [])
    monkeypatch.setattr(probe, 'compare_transports', lambda streams: {
        'available': True, 'requested_bytes': 32, 'same_headers': True,
        'httpx': {'mp4_verified': False}, 'curl_cffi': {'mp4_verified': True}})
    client = TestClient(app)
    ordinary = client.get('/api/v1/cinekivu/titles/123/streams?subject_type=2&detail_path=test')
    assert 'transport_diagnostic' not in ordinary.json()['data']
    result = client.get('/api/v1/cinekivu/titles/123/streams?subject_type=2&detail_path=test&probe_transport=true').json()['data']
    assert result['transport_diagnostic']['curl_cffi']['mp4_verified'] is True
    assert result['playable_count'] == 0 and result['url'] is None
