"""Keep cloud lookup failures distinguishable from missing media, without credentials."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from cinekivu_source import api as cine
import cinekivu_bridge as bridge
from moviebox_api_server import app


@pytest.mark.parametrize('status,code', [(403, None), (200, 460), (200, 0)])
def test_empty_lookup_keeps_safe_web_diagnostics(monkeypatch, status, code):
    monkeypatch.setattr(cine, 'WEB_DOMAINS', ['https://web.example.test'])
    engine = object.__new__(cine.MovieBoxEngine)
    engine.bootstrap_token = lambda: 'private-token'
    engine.session = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
        status_code=status, json=lambda: {'code': code, 'msg': 'private-cookie', 'data': {}}))
    engine.native_play_info = lambda *args: {}
    engine._resource_streams = lambda *args: ([], None)
    result = engine.get_streams('123', detail_path='series', prefer_web_mp4=True)
    assert result == []
    assert result.diagnostics[0]['http_status'] == status
    if code is not None:
        assert result.diagnostics[0]['code'] == code
    assert 'private-' not in str(result.diagnostics)
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *args, **kw: result)
    response = TestClient(app).get('/api/v1/cinekivu/titles/123/streams', params={
        'subject_type': 2, 'detail_path': 'series'})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['playable_count'] == 0 and data['issues']
    assert data['lookup_diagnostics'] == result.diagnostics
    assert 'private-' not in response.text


def test_native_business_rejection_is_not_a_successful_empty_stream_list():
    engine = object.__new__(cine.MovieBoxEngine)
    engine.api_request = lambda *args, **kw: {'code': 460, 'msg': 'private-cookie', 'data': {}}
    with pytest.raises(HTTPException) as error:
        engine.native_play_info('123', 1, 1)
    assert '460' in error.value.detail
    assert 'private-cookie' not in error.value.detail


def test_native_business_rejection_preserves_legacy_route_fallback():
    engine = object.__new__(cine.MovieBoxEngine)
    def request(method, path):
        if 'play-info/v2' in path:
            return {'code': 460, 'data': {}}
        return {'code': 0, 'data': {'streams': [{'url': 'https://cdn.example.test/video.mp4'}]}}
    engine.api_request = request
    assert engine.native_play_info('123', 1, 1)['streams']
