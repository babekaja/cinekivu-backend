"""Historical catalogue parity and separation from current availability."""
import pytest
from fastapi.testclient import TestClient
import moviebox_api_server as server
import cinekivu_reference as reference


@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Reference metadata must work offline')
    monkeypatch.setattr(server, 'get_session', forbidden)
    return TestClient(server.app)


@pytest.mark.parametrize('route', ['taxonomy', 'stats', 'categories', 'genres', 'formats',
                                    'collections', 'languages', 'sources'])
def test_reference_metadata_is_historical_and_stateless(client, route):
    response = client.get('/api/v1/reference/' + route)
    assert response.status_code == 200
    data = response.json()['data']
    assert data['historical'] is True
    assert data['sourceDate'] == '2026-10-15'
    assert data['streamAvailability'] == 'unknown'
    assert 'set-cookie' not in response.headers


def test_complete_reference_and_edition_links(client):
    response = client.get('/api/v1/reference/search?page_size=100')
    data = response.json()['data']
    assert data['total'] == 601 and len(data['items']) == 100 and data['hasMore']
    work = client.get('/api/v1/reference/works/WRK-00001').json()['data']
    assert work['work']['external_ids']['moviebox'] == ['4994654846008578576']
    editions = client.get('/api/v1/reference/works/WRK-00001/editions').json()['data']['items']
    assert editions[0]['id'] == 'EDT-00001'
    assert editions[0]['status_availability'] == 'metadata_seulement'


def test_search_filters_year_category_and_verified_languages(client):
    base = '/api/v1/reference/search'
    data = client.get(base, params={'category': 'series', 'year_min': 2026, 'year_max': 2026}).json()['data']
    assert data['items']
    assert all(x['year'] == 2026 and 'series' in x['categories'] for x in data['items'])
    assert client.get(base, params={'verified_audio': True}).json()['data']['total'] == 0
    assert client.get(base, params={'authorized_playback': True}).json()['data']['total'] == 0
    assert client.get(base, params={'audio_language': 'fr'}).json()['data']['items'] == []
    assert client.get(base, params={'subtitle_language': 'fr'}).json()['data']['items'] == []
    assert client.get(base, params={'page': 1000}).json()['data']['items'] == []
    # A title marked as French is never treated as a verified French audio track.
    assert all(not x['audioVerified'] for x in client.get(base).json()['data']['items'])
    movies = client.get(base, params={'type': 'film'}).json()['data']
    assert movies['total'] > 0


@pytest.mark.parametrize('section', ['tracks', 'seasons', 'episodes', 'volumes', 'chapters', 'rights'])
def test_edition_sections(client, section):
    response = client.get('/api/v1/reference/editions/EDT-00001/' + section)
    assert response.status_code == 200
    assert response.json()['data']['editionId'] == 'EDT-00001'
    if section == 'tracks':
        assert response.json()['data']['value']['audio'] == []


def test_similar_by_genres_excludes_current_work(client):
    data = client.get('/api/v1/reference/works/WRK-00001/recommendations?limit=5').json()['data']
    assert data['items'] and len(data['items']) <= 5
    assert data['method'] == 'shared-genres'
    assert all(x['id'] != 'WRK-00001' and x['sharedGenres'] > 0 for x in data['items'])
    assert [x['sharedGenres'] for x in data['items']] == sorted(
        [x['sharedGenres'] for x in data['items']], reverse=True)


@pytest.mark.parametrize('suffix,status', [
    ('search?page=0', 422), ('search?page_size=101', 422),
    ('search?category=invalid', 422), ('search?genre=invalid', 422),
    ('search?year_min=2026&year_max=2020', 422),
    ('works/unknown', 404), ('editions/unknown', 404),
    ('editions/EDT-00001/play', 422), ('locales/unknown', 404),
])
def test_invalid_reference_queries(client, suffix, status):
    assert client.get('/api/v1/reference/' + suffix).status_code == status


def test_locale_translations_and_missing_resource(client, monkeypatch):
    profile = next(iter(reference.catalogue()['locales']['profils_preferences']))
    assert client.get('/api/v1/reference/locales/' + profile).json()['data']['profile']
    reference.catalogue.cache_clear()
    monkeypatch.setattr(reference, 'DATA', reference.DATA / 'missing')
    try:
        assert client.get('/api/v1/reference/stats').status_code == 503
    finally:
        reference.catalogue.cache_clear()
