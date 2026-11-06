"""The catalogue ranking view must filter only provider-supplied metadata."""
from fastapi.testclient import TestClient
import cinekivu_web
from cinekivu_web import filter_ranked_items
from moviebox_api_server import app

TEST_HOME = {'code': 0, 'data': {'operatingList': [
    {'type': 'SUBJECTS_MOVIE', 'opId': 'popular-shows', 'title': 'Popular Movies', 'subjects': [
        {'subjectId': 'source001', 'title': 'Cinema A', 'subjectType': 1,
         'genre': 'Action', 'countryName': 'France', 'releaseDate': '2026-03-01', 'score': '7.2'},
        {'subjectId': 'source002', 'title': 'Cinema B', 'subjectType': 1,
         'genre': 'Drama', 'countryName': 'Nigeria', 'releaseDate': '2025-01-01', 'score': '8.1'},
        {'subjectId': 'source003', 'title': 'TV C', 'subjectType': 2,
         'genre': 'Animation', 'countryName': 'France', 'releaseDate': '2026-04-01', 'score': '6.9'},
    ]}
]}}

def fake_upstream(method, route, **kwargs):
    if route in ('/home', '/tab-operating'): return TEST_HOME
    if route == '/subject/trending':
        return {'code': 0, 'data': {'items': TEST_HOME['data']['operatingList'][0]['subjects'],
                                  'pager': {'hasMore': False}}}
    raise AssertionError(route)

def test_ranking_filters_strict_and_stable():
    items = cinekivu_web.normalize_home(TEST_HOME)['sections'][0]['items']
    assert [x['subjectId'] for x in filter_ranked_items(items, category='movies')] == ['source001', 'source002']
    assert [x['subjectId'] for x in filter_ranked_items(items, country='France')] == ['source001', 'source003']
    assert [x['subjectId'] for x in filter_ranked_items(items, genre='Action', year='2026')] == ['source001']
    assert [x['subjectId'] for x in filter_ranked_items(items, category='movies', sort='rating')] == ['source002', 'source001']

def test_home_voir_plus_ranking_filters_real_rail(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'upstream', fake_upstream)
    cli = TestClient(app)
    response = cli.get('/api/v1/web/home/rails/popular-shows?category=movies&year=2026')
    assert response.status_code == 200
    payload = response.json()['data']
    assert [x['subjectId'] for x in payload['items']] == ['source001']
    assert payload['totalLiveItems'] == 1
    assert payload['hasMore'] is False
    assert payload['partial'] is True
    assert cli.get('/api/v1/web/home/rails/unknown').status_code == 404
    assert cli.get('/api/v1/web/home/rails/popular-shows?year=wrong').status_code == 422

def test_trending_facets_retain_source_order(monkeypatch):
    monkeypatch.setattr(cinekivu_web, 'upstream', fake_upstream)
    cli = TestClient(app)
    response = cli.get('/api/v1/web/categories/trending?genre=Action')
    assert response.status_code == 200
    assert [m['subjectId'] for m in response.json()['data']['items']] == ['source001']
