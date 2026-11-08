"""SDK workflows against deterministic providers; real HTTP relay is tested separately."""
import time
from collections import OrderedDict

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import cinekivu_downloads as downloads
import cinekivu_bridge as bridge
from cinekivu_source import api as cine, delivery
import moviebox_api_server as server

SRT = b'1\n00:00:01,000 --> 00:00:03,000\nBonjour CineKivu\n'
MEDIA = b'\x00\x00\x00\x18ftypisom' + b'x' * 1012


@pytest.fixture
def setup(monkeypatch):
    calls = {'streams': [], 'captions': [], 'details': [], 'seasons': []}
    monkeypatch.setattr(downloads, '_assets', OrderedDict())
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda u: u.startswith('https://cdn.example.org/'))
    def details(sid):
        calls['details'].append(sid)
        return {'subject_id': sid, 'title': 'Série ../ Exemple' if sid == 'series' else 'Avatar',
                'subject_type': 2 if sid == 'series' else 1, 'release_date': '2009-12-10',
                'detail_path': 'avatar-fixture'}
    def streams(sid, **kwargs):
        calls['streams'].append((sid, kwargs))
        if kwargs.get('episode') == 3:
            return []
        return [{'format': 'MP4', 'quality': f'{quality}p', 'quality_value': quality,
                 'url': f'https://cdn.example.org/{quality}.mp4', 'codec': 'h264',
                 'source': 'fixture', 'headers': {'Cookie': 'PRIVATE_PROVIDER_COOKIE'}}
                for quality in (360, 720, 1080)]
    def captions(sid, **kwargs):
        calls['captions'].append((sid, kwargs))
        return [{'id': 'fr', 'code': 'fr', 'language': 'Français', 'url': 'https://cdn.example.org/fr.srt'},
                {'id': 'en', 'code': 'en', 'language': 'English', 'url': 'https://cdn.example.org/en.srt'}]
    def seasons(sid):
        calls['seasons'].append(sid)
        return [{'season': 1, 'episode_count': 4}]
    monkeypatch.setattr(cine.engine, 'get_details', details)
    monkeypatch.setattr(cine.engine, 'get_streams', streams)
    monkeypatch.setattr(cine.engine, 'get_subtitles', captions)
    monkeypatch.setattr(cine.engine, 'get_episodes', seasons)
    def search(query, page=1, per_page=20):
        # Match the live provider's page-size contract; the SDK's 30 gets 400.
        if per_page > 20:
            raise HTTPException(502, 'API returned HTTP 400')
        return [
            {'subject_id': 'wrong-year', 'title': 'Avatar', 'subject_type': 1, 'release_date': '2022-12-01'},
            {'subject_id': 'correct-year', 'title': 'Avatar', 'subject_type': 1, 'release_date': '2009-12-10'}]
    monkeypatch.setattr(cine.engine, 'search', search)
    monkeypatch.setattr(delivery, 'prepare_streams', lambda values: [
        {**v, 'play_url': delivery._register_mp4_playback(v)} for v in values])
    def forbidden(*a, **kw):
        raise AssertionError('Download metadata must not bootstrap a separate user session')
    monkeypatch.setattr(server, 'get_session', forbidden)
    return TestClient(server.app), calls


def test_capabilities_do_not_probe_provider(setup):
    client, calls = setup
    data = client.get('/api/v1/downloads/capabilities').json()['data']
    assert 'WORST' in data['qualities'] and data['subtitleFormats'] == ['vtt', 'srt']
    assert data['maxBatchEpisodes'] == 20 and data['mirrorsProbed'] is False
    assert all(not calls[key] for key in calls)


@pytest.mark.parametrize('requested,selected', [('BEST', 1080), ('worst', 360), ('720P', 720)])
def test_quality_selection_is_exact_and_never_exposes_cdn_data(setup, requested, selected):
    client, calls = setup
    response = client.post('/api/v1/downloads/resolve', json={'subject_id': 'movie', 'quality': requested})
    assert response.status_code == 200
    data = response.json()['data']
    assert data['file']['qualityValue'] == selected
    assert data['file']['url'].startswith('/api/v1/downloads/assets/')
    assert data['file']['resumeUrl'].startswith('/api/download/by-id/')
    assert 'cdn.example.org' not in str(data) and 'PRIVATE_PROVIDER_COOKIE' not in str(data)
    assert data['availableQualities'] == [360, 720, 1080]
    assert calls['streams'][0][1]['season'] == 0 and calls['streams'][0][1]['episode'] == 0
    assert not calls['captions'] and 'set-cookie' not in response.headers


def test_numeric_quality_not_available_returns_409(setup):
    client, _ = setup
    response = client.post('/api/v1/downloads/resolve', json={'subject_id': 'movie', 'quality': '480p'})
    assert response.status_code == 409
    assert response.json()['detail'] == {'code': 'quality_unavailable', 'availableQualities': [360, 720, 1080]}


def test_auto_resolution_respects_year_instead_of_first_search_result(setup):
    client, calls = setup
    data = client.post('/api/v1/downloads/resolve', json={'query': 'Avatar', 'year': 2009}).json()['data']
    assert data['subjectId'] == 'correct-year' and data['year'] == '2009'
    assert calls['streams'][0][0] == 'correct-year'
    assert client.post('/api/v1/downloads/resolve', json={'query': 'Avatar', 'year': 1999}).status_code == 404


def test_caption_only_skips_video_and_reports_missing_language(setup):
    client, calls = setup
    data = client.post('/api/v1/downloads/resolve', json={
        'subject_id': 'movie', 'captions_only': True,
        'caption_languages': ['French', 'fr', 'ja'], 'caption_format': 'srt'}).json()['data']
    assert data['file'] is None and not calls['streams']
    assert len(data['captions']) == 1 and data['captions'][0]['filename'].endswith('fr.srt')
    assert data['missingCaptionLanguages'] == ['ja'] and data['partial']
    response = client.post('/api/v1/downloads/resolve', json={
        'subject_id': 'movie', 'captions_only': True, 'caption_languages': ['ja']})
    assert response.status_code == 409


def test_batch_keeps_order_and_partial_failures_with_shared_metadata(setup):
    client, calls = setup
    data = client.post('/api/v1/downloads/batch', json={
        'subject_id': 'series', 'episodes': [2, 3, 1], 'quality': '720p'}).json()['data']
    assert [i['episode'] for i in data['items']] == [2, 3, 1]
    assert [i['status'] for i in data['items']] == ['ready', 'unavailable', 'ready']
    assert data['readyCount'] == 2 and data['partial']
    assert calls['details'] == ['series'] and calls['seasons'] == ['series']
    assert 'S01E02' in data['items'][0]['data']['file']['filename']
    assert '/' not in data['items'][0]['data']['file']['filename']


def test_caption_region_request_preserves_actual_declared_language(setup):
    client, _ = setup
    data = client.post('/api/v1/downloads/resolve', json={
        'subject_id': 'movie', 'captions_only': True, 'caption_languages': ['fr-CA']}).json()['data']
    assert data['captions'][0]['requestedLanguage'] == 'fr-ca'
    assert data['captions'][0]['language'] == 'fr'


def test_batch_range_and_season_validation(setup):
    client, calls = setup
    data = client.post('/api/v1/downloads/batch', json={
        'subject_id': 'series', 'start_episode': 1, 'limit': 2}).json()['data']
    assert data['total'] == 2 and data['readyCount'] == 2 and not data['partial']
    before = len(calls['streams'])
    assert client.post('/api/v1/downloads/batch', json={'subject_id': 'series', 'episodes': [5]}).status_code == 422
    assert client.post('/api/v1/downloads/batch', json={'subject_id': 'series', 'season': 2}).status_code == 404
    assert len(calls['streams']) == before


@pytest.mark.parametrize('body', [
    {}, {'subject_id': 'x', 'query': 'Avatar'}, {'query': '   '},
    {'subject_id': '../x'}, {'subject_id': 'x', 'quality': '144p'},
    {'subject_id': 'x', 'year': 1000}, {'subject_id': 'x', 'season': 0},
    {'subject_id': 'x', 'episode': True}, {'subject_id': 'x', 'captions_only': True},
    {'subject_id': 'x', 'caption_languages': ['../fr']}, {'subject_id': 'x', 'url': 'https://evil.example/file'},
])
def test_invalid_resolve_inputs_never_call_provider(setup, body):
    client, calls = setup
    assert client.post('/api/v1/downloads/resolve', json=body).status_code == 422
    assert all(not calls[key] for key in calls)


@pytest.mark.parametrize('body', [
    {'subject_id': 'series', 'episodes': [1, 1]}, {'subject_id': 'series', 'episodes': []},
    {'subject_id': 'series', 'episodes': [1], 'limit': 1},
    {'subject_id': 'series', 'episodes': list(range(1, 22))},
    {'subject_id': 'series', 'start_episode': 9999, 'limit': 2},
])
def test_invalid_batch_inputs_never_call_provider(setup, body):
    client, calls = setup
    assert client.post('/api/v1/downloads/batch', json=body).status_code == 422
    assert all(not calls[key] for key in calls)


def test_asset_registry_expiry_bounds_and_filename_sanitizing(monkeypatch):
    monkeypatch.setattr(downloads, '_assets', OrderedDict())
    monkeypatch.setattr(downloads, 'MAX_ASSETS', 2)
    first = downloads.register_asset(kind='video')
    downloads.register_asset(kind='video')
    last = downloads.register_asset(kind='video')
    assert len(downloads._assets) == 2
    with pytest.raises(HTTPException):
        downloads.get_asset(first.rsplit('/', 1)[-1])
    token = last.rsplit('/', 1)[-1]
    downloads._assets[token]['expires'] = time.monotonic() - 1
    with pytest.raises(HTTPException):
        downloads.get_asset(token)
    assert '\r' not in downloads.filename('../film\r\n', '2020', 0, 0, '720p', 'mp4')


def test_srt_conversion_preserves_text_and_drops_vtt_cue_settings():
    raw = b'WEBVTT\n\nNOTE ignored\nnever show\n\ncue-id\n00:01.000 --> 00:03.200 align:start\nBonjour\nmonde\n'
    value = downloads.convert_to_srt(raw)
    assert value == b'1\n00:00:01,000 --> 00:00:03,200\nBonjour\nmonde\n'
    assert downloads.convert_to_srt(SRT) == SRT


def test_caption_attachment_get_and_head_and_invalid_source(setup, monkeypatch):
    client, _ = setup
    async def read(_):
        return SRT
    monkeypatch.setattr(bridge, 'read_caption', read)
    data = client.post('/api/v1/downloads/resolve', json={
        'subject_id': 'movie', 'captions_only': True,
        'caption_languages': ['fr'], 'caption_format': 'srt'}).json()['data']
    url = data['captions'][0]['url']
    response = client.get(url)
    assert response.content == SRT and 'filename*=UTF-8' in response.headers['content-disposition']
    assert client.head(url).content == b''
    assert client.head(url).headers['content-length'] == str(len(SRT))
    async def invalid(_):
        return b'invalid subtitle'
    monkeypatch.setattr(bridge, 'read_caption', invalid)
    assert client.get(url).status_code == 502
    assert client.get('/api/v1/downloads/assets/unknown').status_code == 404


def test_mp4_asset_uses_existing_range_relay(setup, monkeypatch):
    client, _ = setup
    data = client.post('/api/v1/downloads/resolve', json={'subject_id': 'movie', 'quality': '720p'}).json()['data']
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers['range'] == 'bytes=8-15'
        assert request.headers['if-range'] == '"same-file"'
        return httpx.Response(206, stream=httpx.ByteStream(MEDIA[8:16]), headers={
            'content-type': 'video/mp4', 'content-range': f'bytes 8-15/{len(MEDIA)}',
            'content-length': '8', 'etag': '"same-file"', 'accept-ranges': 'bytes'})
    with TestClient(server.app) as active:
        transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        monkeypatch.setattr(server.app.state, 'cdn_client', transport, raising=False)
        response = active.get(data['file']['url'], headers={'Range': 'bytes=8-15', 'If-Range': '"same-file"'})
        assert response.status_code == 206 and response.content == MEDIA[8:16]
        assert response.headers['content-range'] == f'bytes 8-15/{len(MEDIA)}'
        assert 'filename*=UTF-8' in response.headers['content-disposition']
        active.portal.call(transport.aclose)
    assert len(seen) == 1
