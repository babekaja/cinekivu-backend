"""HTTP transport fixtures verify request contracts and failure handling.

These tests simulate provider responses; live checks are reported separately.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

import cinekivu_enrichment as enrich
from cinekivu_metadata_cache import MetadataCache
import moviebox_api_server as server


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(enrich, 'cache', MetadataCache(ttl_seconds=600, max_entries=128))
    def forbidden(*args, **kwargs):
        raise AssertionError('Enrichment must not create a MovieBox session')
    monkeypatch.setattr(server, 'get_session', forbidden)
    original = httpx.Client
    def transport(handler):
        monkeypatch.setattr(enrich.httpx, 'Client', lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(handler)))
    return TestClient(server.app), transport


def test_anilist_request_and_normalized_metadata(setup):
    client, transport = setup
    seen = []
    def respond(request):
        import json
        seen.append(request)
        body = json.loads(request.content)
        assert request.method == 'POST' and request.url.host == 'graphql.anilist.co'
        assert body['variables'] == {'name': 'Naruto', 'type': 'ANIME', 'limit': 2}
        return httpx.Response(200, json={'data': {'Page': {'media': [
            {'id': 20, 'title': {'english': 'Naruto', 'romaji': 'Naruto'},
             'format': 'TV', 'episodes': 220, 'siteUrl': 'https://anilist.co/anime/20'}]}}})
    transport(respond)
    route = '/api/v1/enrichment/search?provider=anilist&kind=anime&q=Naruto&limit=2'
    response = client.get(route)
    assert response.status_code == 200
    data = response.json()['data']
    assert data['items'][0]['externalId'] == '20'
    assert data['items'][0]['episodes'] == 220
    assert data['streamAvailability'] == 'unknown'
    assert 'subjectId' not in data['items'][0]
    assert 'set-cookie' not in response.headers
    client.get(route)
    assert len(seen) == 1


def test_mangadex_contract_and_language_metadata(setup):
    client, transport = setup
    def respond(request):
        assert request.url.host == 'api.mangadex.org'
        assert request.url.params['title'] == 'Naruto'
        assert request.url.params['contentRating[]'] == 'safe'
        return httpx.Response(200, json={'data': [{'id': 'manga-1', 'attributes': {
            'title': {'en': 'Naruto'}, 'availableTranslatedLanguages': ['fr', 'en']}}]})
    transport(respond)
    data = client.get('/api/v1/enrichment/search?provider=mangadex&kind=manga&q=Naruto').json()['data']
    assert data['items'][0]['url'] == 'https://mangadex.org/title/manga-1'
    assert data['items'][0]['translatedLanguages'] == ['fr', 'en']


@pytest.mark.parametrize('mode,expected', [('timeout', 504), ('rate_limit', 503),
                                          ('http_error', 502), ('malformed', 502), ('graphql_error', 502)])
def test_upstream_errors_are_explicit_and_not_cached(setup, mode, expected):
    client, transport = setup
    seen = []
    def respond(request):
        seen.append(request)
        if mode == 'timeout':
            raise httpx.ReadTimeout('simulated timeout', request=request)
        if mode == 'rate_limit':
            return httpx.Response(429)
        if mode == 'http_error':
            return httpx.Response(500, text='upstream error')
        if mode == 'graphql_error':
            return httpx.Response(200, json={'errors': [{'message': 'unavailable'}]})
        return httpx.Response(200, text='<html>invalid</html>')
    transport(respond)
    route = '/api/v1/enrichment/search?provider=anilist&kind=manga&q=Naruto'
    assert client.get(route).status_code == expected
    assert client.get(route).status_code == expected
    assert len(seen) == 2


@pytest.mark.parametrize('query', [
    'provider=unknown&kind=manga&q=Naruto', 'provider=mangadex&kind=anime&q=Naruto',
    'provider=anilist&kind=book&q=Naruto', 'provider=anilist&kind=anime&q=%20%20',
    'provider=anilist&kind=anime&q=Naruto&limit=6',
])
def test_invalid_requests_never_contact_providers(setup, query):
    client, transport = setup
    transport(lambda _: (_ for _ in ()).throw(AssertionError('no HTTP request expected')))
    assert client.get('/api/v1/enrichment/search?' + query).status_code == 422
