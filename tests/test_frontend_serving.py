from fastapi.testclient import TestClient
from pathlib import Path
from moviebox_api_server import app


def test_frontend_is_served_with_current_api_client():
    response = TestClient(app).get('/app/')
    if not (Path(__file__).resolve().parents[2] / 'web-release').is_dir():
        # The backend repository also runs without the optional sibling frontend.
        assert response.status_code == 404
        return
    assert response.status_code == 200
    assert 'api-client.js' in response.text and 'services-ui.js' in response.text
    assert 'catalogue.js' not in response.text


def test_credentialed_local_frontend_preflight_and_untrusted_origin():
    client = TestClient(app)
    for origin, allowed in [('http://127.0.0.1:5173', True), ('http://localhost:8080', True),
                            ('https://untrusted.example.org', False)]:
        response = client.options('/api/v1/library/123', headers={
            'Origin': origin, 'Access-Control-Request-Method': 'PUT',
            'Access-Control-Request-Headers': 'content-type'})
        assert (response.status_code == 200) is allowed
        if allowed:
            assert response.headers['access-control-allow-origin'] == origin
            assert response.headers['access-control-allow-credentials'] == 'true'
    response = client.get('/health', headers={'Origin': 'http://127.0.0.1:5173'})
    assert 'content-range' in response.headers['access-control-expose-headers'].lower()
