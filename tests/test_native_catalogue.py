"""Contracts observed on movieboxhd.net/fr/web/tv-series, 2026-10-28."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cinekivu_web as web
from cinekivu_native_catalogue import decode_filter_config
from moviebox_api_server import app


def test_all_facets_use_native_ids_and_preserve_provider_order(monkeypatch):
    calls = []
    def source(method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {'code': 0, 'data': {'items': [
            {'subjectId': 'en001', 'subjectType': 2, 'title': 'Z first', 'releaseDate': '2019-01-02'},
            {'subjectId': 'fr002', 'subjectType': 2, 'title': 'A [Version française]', 'releaseDate': '2011-02-03'}],
            'pager': {'page': '2', 'hasMore': True, 'nextPage': '3', 'totalCount': 1000000}}}
    monkeypatch.setattr(web, 'upstream', source)
    result = TestClient(app).post('/api/v1/web/filter', json={
        'category': 'series', 'genre': 'Comédie', 'country': 'France', 'year': '2010s',
        'language': 'French dub', 'sort': 'latest', 'page': 2, 'rate': [7, 10]})
    assert result.status_code == 200
    assert calls == [('POST', '/subject/filter', {'body': {
        'channelId': 2, 'page': 2, 'perPage': 30, 'genre': 'Comedy', 'country': 'France',
        'year': '2010s', 'classify': 'French dub', 'sort': 'Latest', 'rate': ['7.0', '10.0']}})]
    data = result.json()['data']
    assert [x['subjectId'] for x in data['items']] == ['en001', 'fr002']
    assert data['page'] == 2 and data['hasMore'] and data['nextPage'] == '3'
    assert data['totalLiveItems'] is None and data['pager']['totalCount'] is None
    assert data['items'][0]['releaseDate'] == '2019-01-02'
    assert data['partial'] is False and data['stale'] is False


@pytest.mark.parametrize('category,channel', [('movies', 1), ('series', 2), ('anime', 1006)])
def test_native_channel_and_unknown_facet_rejection(monkeypatch, category, channel):
    seen = []
    def source(method, route, **kwargs):
        seen.append(kwargs['body'])
        return {'code': 0, 'data': {'items': [], 'pager': {'hasMore': False, 'totalCount': 0}}}
    monkeypatch.setattr(web, 'upstream', source)
    cli = TestClient(app)
    result = cli.post('/api/v1/web/filter', json={'category': category, 'language': 'Hindi dub', 'year': 'Other'})
    assert result.status_code == 200 and seen[-1]['channelId'] == channel
    assert seen[-1]['classify'] == 'Hindi dub'
    assert cli.post('/api/v1/web/filter', json={'category': category, 'page': 400}).status_code == 200
    assert seen[-1]['page'] == 400
    if category == 'movies':
        assert cli.get('/api/v1/catalog/movies?page=400').status_code == 200
    assert cli.post('/api/v1/web/filter', json={'category': category, 'country': 'Invented'}).status_code == 422
    assert cli.post('/api/v1/web/filter', json={'category': category, 'rate': [9, 3]}).status_code == 422
    assert cli.post('/api/v1/web/filter', json={'category': category, 'rate': [5]}).status_code == 422


def test_options_preserve_labels_decades_and_language_catalogue():
    data = TestClient(app).get('/api/v1/web/filter/options?category=series').json()['data']
    assert {'value': 'Comedy', 'label': 'Comédie'} in data['genres']
    assert {'value': '2010s', 'label': '2010s'} in data['years']
    assert {'value': 'Hindi dub', 'label': 'Doublage hindi'} in data['languages']
    assert data['ratingRange'] == {'minVal': '0', 'maxVal': '10'}
    assert not data['partial'] and not data['stale']


def test_nuxt_decoder_reads_only_config_subtree_and_never_executes_scripts():
    # Nuxt uses references into a flat JSON table, including -1 for null/undefined.
    nodes = [None, {'typeList': 2, 'mobile': 9}, [3],
             {'channelId': 4, 'channelName': 5, 'items': 6}, 2, 'Série', [7],
             {'filterType': 8, 'filterVals': -1}, 'year', []]
    html = '<script>alert("untrusted")</script><script id="__NUXT_DATA__" type="application/json">' + json.dumps(nodes) + '</script>'
    config = decode_filter_config(html, 2)
    assert config['channelId'] == 2 and config['items'][0]['filterType'] == 'year'
    with pytest.raises(ValueError):
        decode_filter_config(html, 1)
    with pytest.raises(ValueError):
        decode_filter_config('<html>maintenance</html>', 2)


def test_native_filter_failure_does_not_call_editorial_endpoints(monkeypatch):
    routes = []
    def offline(method, route, **kwargs):
        routes.append(route)
        raise HTTPException(502, 'provider offline')
    monkeypatch.setattr(web, 'upstream', offline)
    assert TestClient(app).post('/api/v1/web/filter', json={'category': 'series'}).status_code == 502
    assert routes == ['/subject/filter']


def test_search_respects_native_mobile_page_size(monkeypatch):
    calls = []
    def request(method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {'code': 0, 'data': {'items': [], 'pager': {'page': '2', 'hasMore': True}}}
    monkeypatch.setattr(web.cine.engine, 'api_request', request)
    result = web.native_search('Lupin', 2)
    assert result['data']['pager']['hasMore']
    assert calls == [('POST', '/wefeed-mobile-bff/subject-api/search', {
        'body_dict': {'keyword': 'Lupin', 'page': 2, 'perPage': 20}})]


def test_film_page_matches_observed_native_request_and_excludes_other_types(monkeypatch):
    calls = []
    def provider(method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {'code': 0, 'data': {'items': [
            {'subjectId': 'series1', 'subjectType': 2, 'title': 'Une série'},
            {'subjectId': 'movie2', 'subjectType': 1, 'title': 'Z premier film',
             'releaseDate': '2024-12-25', 'imdbRatingValue': '8.1'},
            {'subjectId': 'clip1', 'subjectType': 6, 'title': 'Un clip'},
            {'subjectId': 'movie1', 'subjectType': 1, 'title': 'A deuxième film',
             'releaseDate': '2024-01-19', 'imdbRatingValue': '6.4'}],
            'pager': {'page': '2', 'hasMore': True, 'nextPage': '3', 'perPage': 28, 'totalCount': 58}}}
    monkeypatch.setattr(web, 'upstream', provider)
    response = TestClient(app).post('/api/v1/web/filter', json={
        'category': 'movies', 'genre': 'Comédie', 'country': 'France', 'year': '2024',
        'language': 'fr', 'sort': 'latest', 'rate': [6, 9], 'page': 2})
    assert response.status_code == 200
    assert calls == [('POST', '/subject/filter', {'body': {
        'channelId': 1, 'page': 2, 'perPage': 28, 'genre': 'Comedy',
        'country': 'France', 'year': '2024', 'classify': 'French dub',
        'sort': 'Latest', 'rate': ['6.0', '9.0']}})]
    data = response.json()['data']
    assert [item['subjectId'] for item in data['items']] == ['movie2', 'movie1']
    assert data['items'][0]['releaseDate'] == '2024-12-25'
    assert data['items'][1]['score'] == '6.4'
    assert data['channelId'] == 1 and data['perPage'] == 28
    assert data['page'] == 2 and data['nextPage'] == '3' and data['hasMore']
    assert data['totalLiveItems'] == 58 and not data['partial']
