"""CineKivu integration tests. Fully offline by design."""
from fastapi.testclient import TestClient
from cinekivu_source import api as cine, delivery
from cinekivu_bridge import parse_home, language_rank, convert_subtitle, _register_captions
from moviebox_api_server import app
import moviebox_api_server
import pytest

client = TestClient(app)


def test_home_keeps_cinekivu_source_and_prioritizes_declared_languages():
    result = client.get('/api/v1/home')
    assert result.status_code == 200
    body = result.json()['data']
    assert body['source'] == 'cinekivu-home'
    titles = body['sections'][0]['items']
    assert [title['subjectId'] for title in titles] == ['m123', 's123', 'hi123']
    assert titles[0]['audioVerified'] is False
    assert 'Asian' not in str(client.get('/api/v1/config').json()['data']['tabs'])


def test_frontend_catalog_home_and_provider_paths():
    assert client.get('/api/v1/catalog/movies').json()['data']['items'][0]['subjectId'] == 'm123'
    assert client.get('/api/v1/catalog/series').json()['data']['items'][0]['subjectId'] == 's123'
    assert client.get('/api/v1/catalog/asian').status_code == 404
    response = client.get('/api/v1/cinekivu/health')
    assert response.status_code == 200
    assert response.json()['data']['languages_priority'] == ['fr', 'en']


def test_language_precedence_does_not_fabricate_french_audio():
    assert language_rank({'title': 'Le film VF'}) == 0
    assert language_rank({'title': 'Exemple', 'language': 'English'}) == 1
    assert language_rank({'title': 'Exemple', 'language': 'unknown'}) == 2
    assert language_rank({'title': 'Exemple', 'language': 'Hindi'}) == 3


def test_cinekivu_search_variants_and_invalid_id(monkeypatch):
    queries = []
    def fake_search(q, page=1, per_page=20):
        queries.append(q)
        return ([{'subject_id':'vf001','title':'Naruto [Version Française]','subject_type':2}]
                if '[Version Française]' in q else
                [{'subject_id':'en001','title':'Naruto','subject_type':2}])
    monkeypatch.setattr(cine.engine, 'search', fake_search)
    from cinekivu_bridge import cine_search
    result = cine_search('Naruto')
    assert result['french_editions'][0]['subjectId'] == 'vf001'
    assert result['french_editions'][0]['audioVerified'] is False
    assert 'Naruto [Version Française]' in queries
    assert client.get('/api/v1/cinekivu/titles/illegal%20id/streams').status_code == 422


def test_subtitle_conversion_srt_and_ass_and_bad_file():
    srt = b'1\r\n00:00:01,120 --> 00:00:03,456\r\nBonjour !\r\n\r\n2\r\n00:00:04,000 --> 00:00:05,000\r\nSalut\r\n'
    result = convert_subtitle(srt)
    assert result.startswith(b'WEBVTT\n')
    assert b'00:00:01.120 --> 00:00:03.456' in result
    assert b'Bonjour !' in result
    ass = b'[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.30,0:00:03.60,Default,,0,0,0,,Bonjour\\Ncomment allez-vous\n'
    converted = convert_subtitle(ass)
    assert b'00:00:01.300 --> 00:00:03.600' in converted
    assert b'comment allez-vous' in converted
    with pytest.raises(ValueError): convert_subtitle(b'not captions')


def test_cinekivu_subtitles_fr_before_english_no_raw_url(monkeypatch):
    monkeypatch.setattr(cine.engine, 'get_subtitles', lambda *a,**kw: [
        {'url':'https://example.org/en.srt','language':'English','code':'en','id': 'one'},
        {'url':'https://example.org/fr.srt','language':'Français','code':'fr','id': 'two'},
    ])
    monkeypatch.setattr(delivery,'_public_https_media_url', lambda url: url.startswith('https://example.org/'))
    result = client.get('/api/v1/cinekivu/titles/m123/subtitles')
    assert result.status_code == 200
    items = result.json()['data']['items']
    assert [row['lang'] for row in items] == ['fr','en']
    assert all(row['url'].startswith('/api/v1/cinekivu/captions/') for row in items)
    assert 'example.org' not in str(items)


def test_cinekivu_streams_no_provider_cookies_or_urls(monkeypatch):
    called = {}
    monkeypatch.setattr(cine.engine,'get_details',lambda sid: {'subject_type':1,'detail_path':'sample'})
    def fake_stream(sid, **options):
        called.update(options)
        return [{'format':'MP4', 'url':'https://cdn.example.org/raw.mp4', 'cookie':'PRIVATE_TOKEN',
                 'quality':'720p','quality_value':720,'headers':{'Cookie':'secret'}}]
    monkeypatch.setattr(cine.engine,'get_streams',fake_stream)
    def fake_prepare(streams):
        return [{**streams[0], 'play_url':'/api/play/local-token'}]
    monkeypatch.setattr(delivery,'prepare_streams',fake_prepare)
    result = client.get('/api/v1/cinekivu/titles/m123/streams')
    assert result.status_code == 200
    data = result.json()['data']
    assert data['url'] == '/api/play/local-token'
    assert 'PRIVATE_TOKEN' not in str(data)
    assert 'cdn.example.org' not in str(data)
    assert 'headers' not in str(data)
    assert called['season'] == 0 and called['episode'] == 0


@pytest.mark.parametrize('kind,season,episode,expected', [(1, 1, 1, (0, 0)), (2, 2, 3, (2, 3))])
def test_catalogue_playback_does_not_depend_on_another_detail_lookup(monkeypatch, kind, season, episode, expected):
    def unavailable_details(*args):
        raise AssertionError('Known catalogue metadata must not require a mobile detail request')
    called = {}
    monkeypatch.setattr(cine.engine, 'get_details', unavailable_details)
    monkeypatch.setattr(cine.engine, 'get_streams', lambda sid, **options: called.update(options) or [])
    monkeypatch.setattr(delivery, 'prepare_streams', lambda values: values)
    response = client.get('/api/v1/cinekivu/titles/123/streams', params={
        'season': season, 'episode': episode, 'detail_path': 'film-catalogue-abc', 'subject_type': kind})
    assert response.status_code == 200
    assert (called['season'], called['episode']) == expected
    assert called['detail_path'] == 'film-catalogue-abc'


@pytest.mark.parametrize('path', ['../film', 'https://other.example/film', 'film?subjectId=other'])
def test_catalogue_detail_hint_cannot_inject_a_url_or_query(path):
    assert client.get('/api/v1/cinekivu/titles/123/streams', params={
        'detail_path': path, 'subject_type': 1}).status_code == 422


def test_legacy_stream_redacts_sensitive_media_data(monkeypatch):
    monkeypatch.setattr(moviebox_api_server,'_get_stream_raw',lambda *a,**kw:
        {'url':'https://cdn.example.org/raw.mp4','cookie':'PRIVATE_COOKIE',
         'headers':{'Cookie':'PRIVATE_COOKIE'},'duration':0,
         'subtitles': [],'qualityList':['720p'],'streamId':'R1'})
    monkeypatch.setattr(delivery,'prepare_streams',lambda streams: [{**streams[0], 'play_url':'/api/play/opaque-token'}])
    response = client.get('/stream/m123')
    assert response.status_code == 200
    result = response.json()
    assert result['url'] == '/api/play/opaque-token'
    assert result['cookie'] is None and result['headers'] == {}
    assert result['duration'] is None
    assert 'PRIVATE_COOKIE' not in response.text


def test_cinekivu_related_versions_from_declared_dubs(monkeypatch):
    monkeypatch.setattr(cine.engine, 'get_details', lambda sid: {
        'subject_id': sid, 'title': 'Naruto', 'subject_type': 2,
        'dubs': [{'subject_id':'naruto-fr', 'lang_name':'Français', 'language':'fr'},
                 {'subject_id':'naruto-en', 'lang_name':'English', 'language':'en'}]})
    monkeypatch.setattr(cine.engine, 'search', lambda *a, **kw: [])
    response = client.get('/api/v1/cinekivu/titles/naruto-original/editions')
    assert response.status_code == 200
    values = response.json()['data']
    assert values['french_editions'][0]['subjectId'] == 'naruto-fr'
    assert values['audio_verified'] is False
    assert all(item['audioVerified'] is False for item in values['items'])


def test_legacy_search_uses_working_native_provider(monkeypatch):
    calls = []
    def native(query, page=1, subject_type=0):
        calls.append((query, page, subject_type))
        return {'items': [{'subjectId': 'avatar001', 'title': 'Avatar'}], 'page': page}
    monkeypatch.setattr('cinekivu_bridge.cine_search', native)
    response = client.get('/search', params={'q': ' Avatar ', 'page': 2, 'type': 1})
    assert response.status_code == 200
    assert response.json()['data']['items'][0]['title'] == 'Avatar'
    assert calls == [('Avatar', 2, 1)]


def test_mp4_download_full_range_head_and_episode_identity(monkeypatch):
    import httpx
    import time
    monkeypatch.setattr(cine.engine, 'get_details', lambda sid: {'subject_type': 2})
    monkeypatch.setattr(cine.engine, 'get_streams', lambda *a, **kw: [
        {'format': 'MP4', 'url': 'https://cdn.example.org/movie.mp4',
         'quality': '720p', 'quality_value': 720, 'headers': {'Referer': 'https://example.org/'}},
        {'format': 'MP4', 'url': 'https://cdn.example.org/movie-480.mp4',
         'quality': '480p', 'quality_value': 480}])
    monkeypatch.setattr(delivery, '_public_https_media_url', lambda url: True)
    monkeypatch.setattr(delivery, '_probe_mp4_playback', lambda *a: None)
    payload = b'\x00\x00\x00\x20ftypisom' + b'0' * 64
    seen = []
    def cdn(request):
        seen.append(request)
        assert request.headers['referer'] == 'https://example.org/'
        if request.headers.get('range') == 'bytes=12-':
            return httpx.Response(206, stream=httpx.ByteStream(payload[12:]), headers={
                'Content-Type': 'video/mp4', 'Content-Length': str(len(payload) - 12),
                'Content-Range': f'bytes 12-{len(payload)-1}/{len(payload)}', 'Accept-Ranges': 'bytes'})
        return httpx.Response(200, stream=httpx.ByteStream(payload), headers={
            'Content-Type': 'application/octet-stream', 'Content-Length': str(len(payload)), 'Accept-Ranges': 'bytes'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(cdn), **kw))
    def sources(episode):
        return client.get('/api/v1/cinekivu/titles/s123/streams', params={'episode': episode}).json()['data']['sources']
    first = sources(1)
    download = first[0]['download']
    assert download['id'] != first[1]['download']['id']
    assert download['id'] == sources(1)[0]['download']['id']
    assert download['id'] != sources(2)[0]['download']['id']
    assert 'cdn.example.org' not in str(download)
    full = client.get(download['url'])
    assert full.status_code == 200 and full.content == payload
    assert full.headers['content-type'] == 'video/mp4'
    assert download['id'] in full.headers['content-disposition']
    head = client.head(download['url'])
    assert head.status_code == 200 and not head.content
    assert int(head.headers['content-length']) == len(payload)
    partial = client.get(download['url'], headers={'Range': 'bytes=12-'})
    assert partial.status_code == 206 and partial.content == payload[12:]
    assert partial.headers['content-range'] == f'bytes 12-{len(payload)-1}/{len(payload)}'
    before = len(seen)
    assert client.get(download['url'], headers={'Range': 'nonsense'}).status_code == 416
    assert client.get(download['url'].rsplit('/', 1)[0] + '/another-film').status_code == 403
    assert len(seen) == before
    token = download['url'].split('/')[3]
    delivery._playback_sources[token]['expires'] = time.monotonic() - 1
    assert client.get(download['url']).status_code == 404


def test_download_rejects_adaptive_unverified_and_unknown_sessions():
    assert delivery.register_download({'format': 'HLS', 'play_url': '/api/adaptive/token/master.m3u8'}, 's123', 1, 1) is None
    assert delivery.register_download({'format': 'MP4', 'play_url': '/api/play/unknown'}, 's123', 1, 1) is None
    assert delivery.register_download({'format': 'MP4', 'play_url': '/api/play/token', 'playback_issue': 'refused'}, 's123', 1, 1) is None
    assert client.get('/api/download/unknown/film').status_code == 404
