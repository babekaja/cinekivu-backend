"""Catalogue caching contracts: expiry, concurrency, isolation and shared routes."""
from concurrent.futures import Future, ThreadPoolExecutor
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cinekivu_metadata_cache as caching
import cinekivu_native_catalogue as native
import cinekivu_web as web
from moviebox_api_server import app


def test_expiry_uses_completion_time_and_returns_independent_copies():
    now = [0.0]
    cache = caching.MetadataCache(ttl_seconds=30, max_entries=2, clock=lambda: now[0])
    calls = []

    def load():
        calls.append(1)
        now[0] += 4  # Slow provider calls must not consume the cache's lifetime.
        return {'items': [{'id': len(calls)}]}

    first = cache.get_or_load('movies', load)
    first['items'][0]['id'] = 'changed'
    now[0] = 33.9
    assert cache.get_or_load('movies', load) == {'items': [{'id': 1}]}
    now[0] = 34
    assert cache.get_or_load('movies', load) == {'items': [{'id': 2}]}
    assert len(calls) == 2


def test_cache_is_bounded_and_keeps_recently_used_entries():
    cache = caching.MetadataCache(ttl_seconds=30, max_entries=2)
    calls = []

    def get(key):
        def load():
            calls.append(key)
            return {'category': key}
        return cache.get_or_load(key, load)

    for key in ('movies', 'anime', 'movies', 'series', 'movies', 'anime'):
        get(key)
    assert calls == ['movies', 'anime', 'series', 'anime']


@pytest.mark.parametrize('fails', [False, True])
def test_identical_requests_share_one_load_without_blocking_other_keys(monkeypatch, fails):
    joined, started, release = threading.Event(), threading.Event(), threading.Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(caching, 'Future', ObservedFuture)
    cache = caching.MetadataCache(ttl_seconds=30, max_entries=3)
    calls = []

    def load():
        calls.append(1)
        started.set()
        assert release.wait(5)
        if fails:
            raise HTTPException(503, 'provider unavailable')
        return {'items': [{'id': 'movie'}]}

    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            first = pool.submit(cache.get_or_load, 'movies', load)
            assert started.wait(2)
            second = pool.submit(cache.get_or_load, 'movies', load)
            assert joined.wait(2)
            other = pool.submit(cache.get_or_load, 'anime', lambda: {'items': []})
            assert other.result(timeout=2) == {'items': []}
        finally:
            release.set()
        if fails:
            for result in (first, second):
                with pytest.raises(HTTPException) as caught:
                    result.result(timeout=2)
                assert caught.value.status_code == 503
            assert cache.get_or_load('movies', lambda: {'recovered': True}) == {'recovered': True}
        else:
            assert first.result(timeout=2) == second.result(timeout=2)
            assert first.result() is not second.result()
    assert len(calls) == 1


@pytest.mark.parametrize('status', [403, 429, 502])
def test_expired_success_is_not_served_on_provider_failure(status):
    now = [0.0]
    cache = caching.MetadataCache(ttl_seconds=30, max_entries=2, clock=lambda: now[0])
    cache.get_or_load('movies', lambda: {'items': ['old']})
    now[0] = 30
    attempts = []

    def fail():
        attempts.append(1)
        raise HTTPException(status, 'provider rejected request')

    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            cache.get_or_load('movies', fail)
        assert caught.value.status_code == status
    assert len(attempts) == 2


def test_options_and_titles_share_config_and_all_sessions_share_public_lists(monkeypatch, native_schema):
    config_calls, list_calls = [], []

    def config(category):
        config_calls.append(category)
        return native_schema(category)

    def source(*args, **kwargs):
        list_calls.append(kwargs['body'])
        return {'code': 0, 'data': {'items': [
            {'subjectId': 'movie', 'title': 'Film', 'subjectType': 1}],
            'pager': {'hasMore': False}}}

    monkeypatch.setattr(native, '_load_filter_config', config)
    monkeypatch.setattr(web, 'native_filter_config', native.native_filter_config)
    monkeypatch.setattr(web, 'upstream', source)
    first, second = TestClient(app), TestClient(app)
    assert first.get('/api/v1/web/filter/options?category=movies').status_code == 200
    one = first.post('/api/v1/web/filter', json={'category': 'movies'})
    two = second.post('/api/v1/web/filter', json={'category': 'movies'})
    assert one.status_code == two.status_code == 200
    assert one.json() == two.json()
    assert config_calls == ['movies'] and len(list_calls) == 1


def test_config_refreshes_after_ten_minutes(monkeypatch, native_schema):
    now, calls = [0.0], []
    monkeypatch.setattr(native, '_filter_config_cache', caching.MetadataCache(
        ttl_seconds=600, max_entries=3, clock=lambda: now[0]))

    def load(category):
        calls.append(category)
        return native_schema(category)

    monkeypatch.setattr(native, '_load_filter_config', load)
    native.native_filter_config('movies')
    now[0] = 599
    native.native_filter_config('movies')
    native.native_filter_config('anime')
    now[0] = 600
    native.native_filter_config('movies')
    assert calls == ['movies', 'anime', 'movies']


def test_catalogue_cache_separates_every_filter_and_shares_equivalent_native_ids(monkeypatch):
    calls = []

    def source(*args, **kwargs):
        calls.append(kwargs['body'])
        return {'code': 0, 'data': {'items': [], 'pager': {}}}

    monkeypatch.setattr(web, 'upstream', source)
    queries = [{}, {'page': 2}, {'genre': 'Comedy'}, {'country': 'France'},
               {'year': '2024'}, {'lang': 'fr'}, {'sort': 'latest'}, {'rate': [7, 10]}]
    for query in queries:
        web.live_filter('movies', **{'page': 1, **query})
    web.live_filter('series', 1)
    web.live_filter('anime', 1)
    assert len(calls) == len(queries) + 2
    web.live_filter('movies', 1, genre='Comédie')
    web.live_filter('movies', 1, lang='French dub')
    web.live_filter('movies', 1)
    assert len(calls) == len(queries) + 2


def test_invalid_catalogue_response_is_not_cached(monkeypatch):
    calls = []

    def source(*args, **kwargs):
        calls.append(1)
        return {'code': 0, 'data': {'items': None}}

    monkeypatch.setattr(web, 'upstream', source)
    for _ in range(2):
        with pytest.raises(HTTPException):
            web.live_filter('movies', 1)
    assert len(calls) == 2
