"""Native lists, ranking families and homepage 'more' links are distinct IDs."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cinekivu_web as web
import cinekivu_rankings as rankings
from moviebox_api_server import app

ANIME = '5563687109313136144'
FILMS = '7760430879155407216'
HOME = {'code': 0, 'data': {'operatingList': [
    {'type': 'SUBJECTS_MOVIE', 'opId': 'home-anime', 'title': 'Animés populaires',
     'genreTopId': ANIME, 'detailPath': 'EISbCzfKZC6', 'subjects': [
         {'subjectId': 'z1', 'title': 'Z first', 'subjectType': 2},
         {'subjectId': 'a1', 'title': 'A [Version française]', 'subjectType': 2}]},
    {'type': 'SUBJECTS_MOVIE', 'opId': 'home-films', 'title': 'Films Tendance',
     'genreTopId': FILMS, 'detailPath': 'K7cUgx3Rgf9', 'subjects': [
         {'subjectId': 'f1', 'title': 'Film', 'subjectType': 1}]}]}}


def provider(calls):
    def source(method, route, **kwargs):
        calls.append((method, route, kwargs))
        if route == '/home':
            return HOME
        assert method == 'GET' and route == '/ranking-list/content'
        page = kwargs['params']['page']
        return {'code': 0, 'data': {'title': 'Liste native', 'subjectList': [
            {'subjectId': 'z1', 'title': 'Z first', 'subjectType': 2, 'imdbRatingValue': '3'},
            {'subjectId': 'a1', 'title': 'A [Version française]', 'subjectType': 2, 'imdbRatingValue': '9'}],
            'pager': {'hasMore': True, 'page': str(page), 'nextPage': str(page+1), 'perPage': 20, 'totalCount': 0}}}
    return source


def test_home_more_keeps_full_list_id_and_original_order():
    rows = web.normalize_home(HOME)['sections']
    assert rows[0]['id'] == 'home-anime' and rows[0]['rankingId'] == ANIME
    assert rows[0]['rankingPath'] == 'EISbCzfKZC6'
    assert [item['subjectId'] for item in rows[0]['items']] == ['z1', 'a1']
    assert rows[1]['rankingId'] == FILMS


def test_native_ranking_uses_twenty_preserves_order_ranks_and_unknown_total(monkeypatch):
    calls = []
    monkeypatch.setattr(web, 'upstream', provider(calls))
    result = TestClient(app).get(f'/api/v1/web/rankings/{ANIME}?page=2')
    assert result.status_code == 200
    assert calls == [('GET', '/ranking-list/content', {'params': {'id': ANIME, 'page': 2, 'perPage': 20}})]
    data = result.json()['data']
    assert [item['subjectId'] for item in data['items']] == ['z1', 'a1']
    assert [item['rank'] for item in data['items']] == [21, 22]
    assert data['perPage'] == 20 and data['hasMore'] and data['nextPage'] == '3'
    assert data['totalLiveItems'] is None and data['pager']['totalCount'] is None
    assert data['rankingId'] == ANIME and not data['partial']


def test_old_home_more_url_and_top10_resolve_native_lists(monkeypatch):
    calls = []
    monkeypatch.setattr(web, 'upstream', provider(calls))
    cli = TestClient(app)
    assert cli.get('/api/v1/web/home/rails/home-anime?page=2').json()['data']['rankingId'] == ANIME
    assert cli.get('/api/v1/web/categories/top10').json()['data']['rankingId'] == FILMS
    assert all(call[1] in ('/home', '/ranking-list/content') for call in calls)


def test_ranking_navigation_uses_provider_families_instead_of_guessed_labels(monkeypatch):
    calls = []
    def read(sid='', path=''):
        calls.append((sid, path))
        current = sid or '9222750553476845776'
        return {'tabs': [{'id': current, 'title': 'Animés' if sid == ANIME else 'Tendances actuelles', 'path': path or 'main'}],
                'currentId': current, 'title': 'Liste', 'stale': False}
    monkeypatch.setattr(rankings, 'read_ranking_navigation', read)
    monkeypatch.setattr(web, 'upstream', provider([]))
    cli = TestClient(app)
    assert cli.get('/api/v1/web/rankings').json()['data']['currentId'] == '9222750553476845776'
    assert cli.get(f'/api/v1/web/rankings?ranking_id={ANIME}').json()['data']['currentId'] == ANIME
    assert calls == [('', ''), (ANIME, 'EISbCzfKZC6')]


def test_invalid_or_failed_ranking_never_falls_back_to_another_list(monkeypatch):
    calls = []
    def unavailable(method, route, **kwargs):
        calls.append(route)
        raise HTTPException(502, 'offline')
    monkeypatch.setattr(web, 'upstream', unavailable)
    cli = TestClient(app)
    assert cli.get(f'/api/v1/web/rankings/{ANIME}').status_code == 502
    assert calls == ['/ranking-list/content']
    assert cli.get('/api/v1/web/rankings?ranking_id=bad').status_code == 422
    with pytest.raises(HTTPException) as error:
        rankings.read_ranking_navigation(ANIME, '../../private')
    assert error.value.status_code == 422
