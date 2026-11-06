"""The V3 catalogue only serves current API responses, never home.json."""
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cinekivu_web
from cinekivu_web import (normalize_home, french_first_home, listing,
                          pager, upstream, FeedbackStore, FeedbackCreate)
from moviebox_api_server import app


HOME = {'code': 0, 'data': {'operatingList': [
    {'type': 'BANNER', 'title': 'En vedette', 'banner': {'items': [
        {'subjectId': 'fr1', 'image': {'url': 'https://example.org/banner.jpg'},
         'subject': {'subjectId': 'fr1', 'title': 'Film [Version française]',
                     'subjectType': 1, 'genre': 'Comedy', 'countryName': 'France',
                     'releaseDate': '2026-02-01', 'detailPath': 'film-fr'}}]}},
    {'type': 'SUBJECTS_MOVIE', 'title': 'New movies', 'opId': 'new-films', 'subjects': [
        {'subjectId': 'movie1', 'title': 'Film actuel', 'subjectType': 1,
         'genre': 'Comedy', 'countryName': 'France', 'releaseDate': '2026-01-01'},
        {'subjectId': 'video1', 'title': 'Clip musical', 'subjectType': 6}]},
    {'type': 'SUBJECTS_MOVIE', 'title': 'Short Series', 'opId': 'short-series', 'subjects': [
        {'subjectId': 'series1', 'title': 'Série actuelle', 'subjectType': 2,
         'genre': 'Animation', 'countryName': 'Nigeria', 'releaseDate': '2025-01-01'}]},
]}}
MOVIES = {'code': 0, 'data': {'operatingList': [
    {'type': 'SUBJECTS_MOVIE', 'title': 'Movies', 'opId': 'movies', 'subjects': [
        {'subjectId': 'movie2', 'title': 'Autre film', 'subjectType': 1,
         'genre': 'Drama', 'countryName': 'Ghana', 'releaseDate': '2024-01-01'}]},
]}}


def provider(method, route, **kwargs):
    if route == '/ranking-list/content':
        return {'code': 0, 'data': {'title': 'Top Films', 'subjectList':
            MOVIES['data']['operatingList'][0]['subjects'], 'pager': {'hasMore': False, 'perPage': 20}}}
    if route == '/subject/filter':
        body = kwargs['body']
        rows = [HOME['data']['operatingList'][0]['banner']['items'][0]['subject'],
                *HOME['data']['operatingList'][1]['subjects'],
                *HOME['data']['operatingList'][2]['subjects'],
                *MOVIES['data']['operatingList'][0]['subjects']]
        kind = {1: 1, 2: 2}.get(body['channelId'])
        rows = [x for x in rows if x['subjectType'] == kind] if kind else [rows[3]]
        if body.get('genre'): rows = [x for x in rows if body['genre'] in x.get('genre', '')]
        if body.get('year'): rows = [x for x in rows if x.get('releaseDate', '').startswith(body['year'])]
        if body.get('country'): rows = [x for x in rows if x.get('countryName') == body['country']]
        if body.get('sort') == 'Latest': rows.sort(key=lambda x: x.get('releaseDate', ''), reverse=True)
        return {'code': 0, 'data': {'items': rows, 'pager': {'page': str(body['page']),
                'nextPage': str(body['page'] + 1), 'hasMore': False, 'totalCount': len(rows)}}}
    if route == '/subject/search':
        return {'code': 0, 'data': {'items': [HOME['data']['operatingList'][1]['subjects'][0]],
                                  'pager': {'hasMore': False}}}
    if route == '/home': return HOME
    if route == '/tab-operating': return MOVIES
    if route == '/subject/detail-rec':
        return {'code': 0, 'data': {'items': [
            {'subjectId': 'movie2', 'title': 'Autre film', 'subjectType': 1}]}}
    raise AssertionError(route)



@pytest.fixture(autouse=True)
def stub_native_search(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'native_search', lambda q, page:
                        cinekivu_web.upstream('POST', '/subject/search', body={'keyword': q, 'page': page}))


def test_live_home_and_type_filter(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'upstream', provider)
    normalized = normalize_home(HOME)
    assert normalized['stale'] is False
    assert normalized['source'] == 'web-bff-live'
    assert normalized['featured']['subjectId'] == 'fr1'
    assert all(row['subjectType'] in (1, 2) for section in normalized['sections']
               for row in section['items'])
    assert french_first_home(normalized)['banners'][0]['subjectId'] == 'fr1'
    response = TestClient(app).get('/api/v1/web/home')
    assert response.status_code == 200
    assert response.json()['data']['stale'] is False


def test_live_filters_facets_categories_and_title(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'upstream', provider)
    monkeypatch.setattr(cinekivu_web.cine.engine, 'get_details', lambda sid:
                        HOME['data']['operatingList'][0]['banner']['items'][0]['subject'])
    client = TestClient(app)
    films = client.post('/api/v1/web/filter', json={'category': 'movies', 'genre': 'Comédie'})
    assert films.status_code == 200
    assert {row['subjectId'] for row in films.json()['data']['items']} == {'fr1', 'movie1'}
    assert films.json()['data']['stale'] is False
    options = client.get('/api/v1/web/filter/options?category=movies').json()['data']
    assert options['source'] == 'web-bff-native-filter-config' and options['stale'] is False
    assert any(row['value'] == 'Comedy' for row in options['genres'])
    assert client.get('/api/v1/web/titles/fr1').json()['data']['source'] == 'cinekivu-live'
    assert client.get('/api/v1/web/categories/new').json()['data']['items']
    assert client.get('/api/v1/web/categories/top10').json()['data']['stale'] is False
    assert [row['subjectId'] for row in client.get('/api/v1/web/categories/anime').json()['data']['items']] == ['series1']
    assert [row['subjectId'] for row in client.get('/api/v1/web/categories/short-series').json()['data']['items']] == ['series1']
    assert client.get('/api/v1/web/categories/unsupported').status_code == 404


def test_outage_never_replays_home_json(monkeypatch):
    def offline(*args, **kwargs): raise HTTPException(502, 'offline')
    monkeypatch.setattr(cinekivu_web, 'upstream', offline)
    client = TestClient(app)
    assert client.get('/api/v1/web/home').status_code == 502
    assert client.post('/api/v1/web/filter', json={'category': 'movies'}).status_code == 502
    assert client.get('/api/v1/web/categories/new').status_code == 502
    assert client.get('/api/v1/web/snapshot/search?q=film').status_code == 502


def test_old_search_url_is_live_and_recommendations_are_direct(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'upstream', provider)
    client = TestClient(app)
    body = client.get('/api/v1/web/snapshot/search?q=film').json()['data']
    assert body['source'] == 'web-bff-native-search' and body['deprecatedRoute'] is True
    assert all('film' in row['title'].casefold() for row in body['items'])
    rows = client.get('/api/v1/web/recommendations/movie1').json()['data']['items']
    assert [row['subjectId'] for row in rows] == ['movie2']


def test_pager_validation_and_fixed_upstream_path():
    sample = {'code': 0, 'data': {'subjectList': [
        {'subjectId': '123', 'subjectType': 1, 'title': 'Example'}],
        'pager': {'totalCount': 0, 'hasMore': True, 'nextPage': '2'}}}
    assert len(listing(sample)) == 1
    assert pager(sample)['hasMore'] is True and pager(sample)['nextPage'] == '2'
    client = TestClient(app)
    assert client.post('/api/v1/web/filter', json={'category': 'illegal'}).status_code == 422
    assert client.post('/api/v1/web/filter', json={'year': 'foobar'}).status_code == 422
    assert client.get('/api/v1/web/rankings/not-an-id').status_code == 422
    assert client.get('/api/v1/web/recommendations/bad%20id').status_code == 422
    with pytest.raises(HTTPException) as exc:
        upstream('GET', '/anything/else')
    assert exc.value.status_code == 422


def test_feedback_rate_limit_and_openapi_ids():
    with tempfile.TemporaryDirectory() as directory:
        store = FeedbackStore(str(Path(directory) / 'feedback.sqlite'))
        item = FeedbackCreate(subject_id='123', category='audio_language',
                              comment='Saison 1, mauvaise piste audio')
        for _ in range(8): store.insert('one', item)
        assert len(store.own('two', '123')) == 0
        with pytest.raises(HTTPException) as exc: store.insert('one', item)
        assert exc.value.status_code == 429
    spec = TestClient(app).get('/openapi.json').json()
    ids = [details['operationId'] for path in spec['paths'].values()
           for details in path.values() if isinstance(details, dict) and 'operationId' in details]
    assert len(ids) == len(set(ids))


def test_v12_movies_series_anime_filters_and_category_sort(monkeypatch):
    """HTTP contract: three menus use provider-backed facets, not decorative chip state."""
    def trending_provider(method, route, **kwargs):
        if route == '/subject/trending':
            return {'code': 0, 'data': {'items': [
                {'subjectId': 'movie2', 'title': 'Autre film', 'subjectType': 1},
                {'subjectId': 'series1', 'title': 'Série actuelle', 'subjectType': 2}],
                'pager': {'hasMore': False}}}
        return provider(method, route, **kwargs)

    monkeypatch.setattr(cinekivu_web, 'upstream', trending_provider)
    client = TestClient(app)
    series = client.post('/api/v1/web/filter', json={
        'category': 'series', 'year': '2025', 'genre': 'Animation', 'sort': 'latest'})
    assert series.status_code == 200
    assert [row['subjectId'] for row in series.json()['data']['items']] == ['series1']
    anime = client.post('/api/v1/web/filter', json={'category': 'anime', 'sort': 'rating'})
    assert anime.status_code == 200
    assert [row['subjectId'] for row in anime.json()['data']['items']] == ['series1']
    assert client.get('/api/v1/web/filter/options?category=anime').status_code == 200
    genre = client.get('/api/v1/web/filter/options?category=anime').json()['data']['genres']
    assert any(row['value'] == 'Animation' for row in genre)
    assert client.post('/api/v1/web/filter', json={'category':'movies','year':'2026'}).json()['data']['totalLiveItems'] == 2
    order = client.post('/api/v1/web/filter', json={'category':'movies','sort':'hottest'}).json()['data']
    assert order['source'] == 'web-bff-native-filter'
    assert client.get('/api/v1/web/categories/trending').json()['data']['items'][0]['subjectId'] == 'movie2'
    assert [item['subjectId'] for item in client.get('/api/v1/web/categories/nollywood').json()['data']['items']] == []
    assert [item['subjectId'] for item in client.get('/api/v1/web/categories/latest').json()['data']['items']] == ['fr1','movie1','movie2']
    assert client.post('/api/v1/web/filter',json={'sort':'unsupported'}).status_code == 422


def test_filter_configuration_outage_has_no_saved_fallback(monkeypatch):
    def offline(*args, **kwargs): raise HTTPException(502, 'offline')
    monkeypatch.setattr(cinekivu_web, 'native_filter_config', offline)
    assert TestClient(app).get('/api/v1/web/filter/options').status_code == 502
