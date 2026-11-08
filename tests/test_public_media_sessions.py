"""Public media routes must not wait for an unrelated personal visitor login."""
import pytest
from fastapi.testclient import TestClient
import moviebox_api_server as server
from cinekivu_source import api as cine


@pytest.mark.parametrize('path,status', [
    ('/api/v1/cinekivu/health', 200),
    ('/api/v1/cinekivu/titles/123', 200),
    ('/api/v1/cinekivu/titles/123/streams?subject_type=1&detail_path=test-film', 200),
    ('/api/play/unknown-render-session', 404),
    ('/api/v1/cinekivu/captions/unknown-render-session.vtt', 404),
])
def test_public_media_does_not_bootstrap_personal_session(monkeypatch, path, status):
    def forbidden(*args, **kwargs):
        raise AssertionError('Personal visitor login must not block public media')
    monkeypatch.setattr(server, 'get_session', forbidden)
    monkeypatch.setattr(cine.engine, 'get_details', lambda sid: {
        'subject_id': sid, 'title': 'Test film', 'subject_type': 1})
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **kw: [])
    response = TestClient(server.app).get(path)
    assert response.status_code == status
    assert 'session_id' not in response.cookies


def test_personal_library_still_initializes_its_session(monkeypatch):
    def initialize(*args, **kwargs):
        raise RuntimeError('personal session required')
    monkeypatch.setattr(server, 'get_session', initialize)
    with pytest.raises(RuntimeError, match='personal session required'):
        TestClient(server.app).get('/api/v1/library')
