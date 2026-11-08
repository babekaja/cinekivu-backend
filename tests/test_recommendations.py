"""Corpus recommendations must remain usable without MovieBox or a session."""
from concurrent.futures import ThreadPoolExecutor
import math

import pytest
from fastapi.testclient import TestClient

import cinekivu_recommendations as rec
import moviebox_api_server as server


@pytest.fixture
def engine():
    return rec.RecommendationEngine([
        rec.Movie(0, 'Mars Mission', ('Science Fiction',), 'astronaut mars rocket space rescue'),
        rec.Movie(1, 'Mars Return', ('Science Fiction',), 'astronaut mars rocket space rescue'),
        rec.Movie(2, 'Space Station', ('Science Fiction', 'Drama'), 'astronaut space station rescue'),
        rec.Movie(7, 'Kitchen Romance', ('Romance',), 'chef restaurant kitchen wedding love'),
        rec.Movie(8, 'Astérix', ('Comedy',), 'gaul roman village adventure battle'),
        rec.Movie(9, 'The Kitchen', ('Drama',), 'chef restaurant kitchen cooking meal'),
    ], min_df=1)


@pytest.fixture
def client(monkeypatch, engine):
    monkeypatch.setattr(rec, 'get_engine', lambda: engine)
    def forbidden(*args, **kwargs):
        raise AssertionError('Local recommendations must not create a provider session')
    monkeypatch.setattr(server, 'get_session', forbidden)
    return TestClient(server.app)


def test_cosine_scores_are_correct_sorted_and_exclude_seed(engine):
    values = engine.recommend(engine.movie(0).plot, 30, exclude=0)
    assert values[0]['movieId'] == 1
    assert math.isclose(values[0]['similarity'], 1.0)
    assert [x['movieId'] for x in values] == [1, 2]
    assert all(0 < x['similarity'] <= 1 for x in values)
    assert [x['similarity'] for x in values] == sorted(
        [x['similarity'] for x in values], reverse=True)
    # The independent three-document calculation checks the actual cosine formula.
    small = rec.RecommendationEngine([
        rec.Movie(0, 'A', (), 'alpha alpha beta'),
        rec.Movie(1, 'B', (), 'alpha beta'),
        rec.Movie(2, 'C', (), 'gamma delta')], min_df=1)
    a = math.log(4 / 3) + 1
    b = math.log(4 / 2) + 1
    expected = 4 * a * a / (math.sqrt(6 * a * a + b * b) * math.sqrt(3 * a * a))
    assert small.recommend('alpha alpha beta', 1, exclude=0)[0]['similarity'] == pytest.approx(expected, abs=1e-6)


def test_unknown_words_do_not_fabricate_recommendations(engine):
    assert engine.recommend('qzxvunknown zzyyunknown') == []
    assert engine.recommend('the and of') == []


def test_index_is_lazy_shared_and_reused_under_concurrent_requests(engine):
    assert engine._index is None
    with ThreadPoolExecutor(max_workers=4) as pool:
        indices = list(pool.map(lambda _: engine._get_index(), range(8)))
    assert all(index is indices[0] for index in indices)


def test_search_handles_accents_pagination_and_genres(client):
    base = '/api/v1/recommender/movies'
    data = client.get(base, params={'q': 'ASTerix'}).json()['data']
    assert [m['movieId'] for m in data['items']] == [8]
    assert data['items'][0]['catalogId'] == 'moviebox-corpus:8'
    assert 'subjectId' not in data['items'][0]
    data = client.get(base, params={'genre': 'science fiction', 'page_size': 2}).json()['data']
    assert data['total'] == 3 and data['hasMore']
    second = client.get(base, params={'genre': 'Science Fiction', 'page_size': 2, 'page': 2}).json()['data']
    assert [m['movieId'] for m in second['items']] == [2]
    assert not second['hasMore']
    assert client.get(base, params={'page': 99}).json()['data']['items'] == []


def test_detail_plots_and_genres(client):
    detail = client.get('/api/v1/recommender/movies/0')
    assert detail.status_code == 200
    assert detail.json()['data']['movie']['plot'].startswith('astronaut')
    assert 'set-cookie' not in detail.headers
    without = client.get('/api/v1/recommender/movies').json()['data']['items']
    assert all('plot' not in m for m in without)
    with_plots = client.get('/api/v1/recommender/movies?include_plots=true').json()['data']['items']
    assert all('plot' in m for m in with_plots)
    genres = client.get('/api/v1/recommender/genres').json()['data']['items']
    assert {'name': 'Science Fiction', 'count': 3} in genres


def test_recommendation_routes_are_stateless_and_mark_availability_unknown(client):
    response = client.get('/api/v1/recommender/movies/0/recommendations?limit=1&include_plots=true')
    assert response.status_code == 200
    data = response.json()['data']
    assert data['items'][0]['movieId'] == 1 and len(data['items']) == 1
    assert data['streamAvailability'] == 'unknown' and data['language'] == 'en'
    assert data['method'] == 'tfidf-cosine'
    assert 'plot' in data['items'][0] and 'plot' in data['seed']
    assert 'set-cookie' not in response.headers
    free = client.post('/api/v1/recommender/recommendations', json={'text': 'chef kitchen restaurant', 'limit': 2})
    assert free.status_code == 200
    assert {m['movieId'] for m in free.json()['data']['items']} == {7, 9}


@pytest.mark.parametrize('path', [
    '/movies/-1', '/movies/abc', '/movies?page=0', '/movies?page_size=101',
    '/movies?q=' + 'a' * 121, '/movies/0/recommendations?limit=0',
    '/movies/0/recommendations?limit=31',
])
def test_invalid_query_values_are_422_without_provider_calls(client, path):
    assert client.get('/api/v1/recommender' + path).status_code == 422


@pytest.mark.parametrize('body', [
    {'text': '   '}, {'text': 'ab'}, {'text': 'a' * 10001},
    {'text': 'space', 'limit': 31}, {'text': 'space', 'limit': True},
    {'text': 'space', 'unknown': 1}, {},
])
def test_invalid_synopsis_is_422(client, body):
    assert client.post('/api/v1/recommender/recommendations', json=body).status_code == 422


def test_missing_movie_and_missing_corpus(client, monkeypatch):
    assert client.get('/api/v1/recommender/movies/5000').status_code == 404
    assert client.get('/api/v1/recommender/movies/5000/recommendations').status_code == 404
    def missing():
        raise FileNotFoundError('missing')
    monkeypatch.setattr(rec, 'get_engine', missing)
    assert client.get('/api/v1/recommender/status').status_code == 503


def test_bundled_corpus_is_complete_with_original_ids():
    engine = rec.RecommendationEngine.from_csv(rec.CORPUS_PATH)
    assert len(engine.movies) == 5000
    assert set(engine.by_id) == set(range(5000))
    assert 'Britannia' in engine.movie(2874).title
    assert engine._index is None


def test_duplicate_corpus_ids_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        rec.RecommendationEngine([rec.Movie(1, 'A', (), 'plot'), rec.Movie(1, 'B', (), 'plot')])
