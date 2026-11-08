"""Remaining API contracts with isolated, explicit account/provider fixtures.

OTP, registration and public posts are simulated and never sent externally.
"""
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pytest

import moviebox_api_server as server
from cinekivu_source import api as cine
import cinekivu_discovery as discovery
import cinekivu_media_source as media_source
import cinekivu_web as web
from test_downloads_sdk import setup  # Reuse the verified-media transport fixture.

SUBJECT = {'subjectId': '123', 'title': 'Audit Film', 'subjectType': 1,
           'cover': {'url': 'https://example.org/film.jpg'}, 'releaseDate': '2020-01-01',
           'staffList': [{'staffId': 'actor1', 'name': 'Acteur de test'}], 'description': 'Test'}
POST = {'postId': 'post1', 'content': 'Commentaire synthétique', 'status': 1,
        'user': {'nickname': 'Compte de test'}, 'stat': {}}
ROOM = {'groupId': 'room1', 'roomId': 'room1', 'title': 'Groupe de test', 'name': 'Groupe de test'}


class Content:
    def get_categories(self, *args, **kwargs):
        return {'code': 0, 'data': {'list': [{'title': 'Sélection', 'subjects': [SUBJECT]}]}}
    def get_rankings(self, *args, **kwargs):
        return {'code': 0, 'data': {'subjects': [SUBJECT]}}
    def get_discovery(self):
        return {'code': 0, 'data': {'items': [SUBJECT]}}
    def get_trending(self):
        return self.get_discovery()
    def get_home_list(self, **kwargs):
        return self.get_discovery()
    def get_movie_detail(self, sid):
        return {'code': 0, 'data': {**SUBJECT, 'subjectId': sid}}
    def get_episode_list(self, sid):
        return {'code': 0, 'data': {'seasons': [{'se': 1, 'maxEp': 3}]}}
    def get_rooms(self, **kwargs):
        return {'code': 0, 'data': {'items': [ROOM]}}
    def get_room_detail(self, sid):
        return {'code': 0, 'data': ROOM}
    def get_search_suggestions(self):
        return {'code': 0, 'data': {'items': [{'keyword': 'Audit Film'}]}}
    def get_search_autocomplete(self, q):
        return {'code': 0, 'data': {'items': [{'keyword': 'Audit Film'}]}}
    def search(self, *args, **kwargs):
        return self.get_discovery()
    def get_recommendations(self, sid):
        return {'code': 0, 'data': {'items': [{**SUBJECT, 'subjectId': '456'}]}}
    def get_detail_recommendations(self, sid):
        return self.get_recommendations(sid)
    def get_related_playback(self, sid):
        return self.get_recommendations(sid)
    def get_staff_titles(self, sid, **kwargs):
        return self.get_recommendations(sid)
    def get_playlist_content(self, sid, **kwargs):
        return self.get_discovery()


class Client:
    def __init__(self):
        self.calls = []
    def request_otp(self, *args):
        self.calls.append(('simulated-otp', args))
        return {'status': 'success', 'msg': 'Simulated; no message sent'}
    def login(self, *args):
        return {'status': 'success', 'user': {'nickname': 'Test'}}
    def register(self, *args):
        return {'status': 'success', 'msg': 'Simulated registration'}
    def logout(self):
        return {'status': 'success'}
    def request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if 'post/list/subject' in endpoint:
            assert method == 'GET' and kwargs['params']['id'] == '123'
            return {'code': 0, 'data': {'items': [POST], 'pager': {'hasMore': False}}}
        if 'post/count' in endpoint:
            return {'code': 0, 'data': {'count': 1}}
        if endpoint.endswith('/get-ext-captions'):
            return {'code': 0, 'data': {'extCaptions': [{'id': 'fr', 'language': 'French'}]}}
        if endpoint.endswith('/subtitle-search'):
            return {'code': 0, 'data': {'items': [{'title': 'Audit captions'}]}}
        if '/group/list/' in endpoint:
            return {'code': 0, 'data': {'items': [ROOM]}}
        if '/interactive/post/list' in endpoint:
            return {'code': 0, 'data': {'items': [POST]}}
        if 'resource-position' in endpoint:
            return {'code': 0, 'data': {'position': 1000}}
        if endpoint.endswith('/get'):
            return {'code': 0, 'data': SUBJECT}
        if endpoint.endswith(('/post/like', '/post/create', '/statistics/user-operation')):
            return {'code': 0, 'data': {'recorded': True}}
        raise AssertionError('Unmodelled provider call: '+endpoint)


class User:
    def get_history(self, **kwargs):
        return {'code': 0, 'data': {'items': []}}
    def get_watchlist(self, **kwargs):
        return {'code': 0, 'data': {'items': [SUBJECT]}}
    def report_history(self, *args, **kwargs):
        return {'code': 0, 'data': {'recorded': True}}
    def toggle_watchlist(self, *args, **kwargs):
        return {'code': 0, 'data': {'recorded': True}}


@pytest.fixture
def account(monkeypatch, tmp_path):
    sid = str(uuid.uuid4())
    client = Client()
    monkeypatch.setitem(server.sessions, sid, {
        'id': sid, 'client': client, 'content': Content(), 'user': User(),
        'auth': SimpleNamespace(is_guest_mode=False, is_logged_in=True, user_info={'nickname': 'Test'})})
    monkeypatch.setattr(server, 'HISTORY_FILE', str(tmp_path/'history.json'))
    monkeypatch.setattr(discovery, 'cine_autocomplete', lambda q: [{'title': 'Audit Film', 'query': 'Audit Film'}])
    monkeypatch.setattr(cine.engine, 'get_details', lambda value: {
        'subject_id': value, 'subject_type': 1, 'title': 'Audit Film', 'release_date': '2020-01-01'})
    monkeypatch.setattr(cine.engine, 'get_episodes', lambda value: [{'season': 1, 'episode_count': 3}])
    async def detail(path):
        return {'subjectId': '123', 'title': 'Audit Film', 'cast': [], 'trailer': {}, 'rating': {}}
    async def reviews(*args):
        return {'items': [], 'hasMore': False, 'nextCursor': None}
    monkeypatch.setattr(media_source.source_client, 'detail', detail)
    monkeypatch.setattr(media_source.source_client, 'reviews', reviews)
    return TestClient(server.app, cookies={'session_id': server._sign_session(sid)}), client, sid


READS = ['/', '/health', '/api/health', '/user-info', '/home', '/anime', '/rankings',
         '/discovery', '/trending', '/movies', '/short-tv', '/kids', '/education', '/music',
         '/asian', '/western', '/nollywood', '/tab/2', '/game', '/search-suggestions?q=Audit',
         '/rooms/recommend', '/rooms/room1', '/detail/123', '/episodes/123',
         '/subtitles/search?query=Audit', '/subtitles/123',
         '/history/position?subject_id=123&resource_id=r1', '/history', '/watchlist',
         '/post/count/123', '/groups/trending', '/groups/interactive', '/post/list/123',
         '/api/v1/cinekivu/titles/123/episodes', '/api/v1/titles/123',
         '/api/v1/titles/123/seasons', '/api/v1/genres', '/api/v1/suggestions?q=Audit',
         '/api/v1/history', '/api/v1/web/feedback/123', '/api/v1/media/123/trailer']


@pytest.mark.parametrize('url', READS)
def test_remaining_read_contracts(account, url):
    client, _, _ = account
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data.get('code', 0) in (0, 200)
    assert data.get('status') != 'error'


@pytest.mark.parametrize('url,body', [
    ('/request-otp', {'account': 'test@example.invalid'}),
    ('/login', {'account': 'test@example.invalid', 'password': 'synthetic-test'}),
    ('/register', {'account': 'test@example.invalid', 'password': 'synthetic-test', 'otp': '000000'}),
    ('/logout', None),
    ('/history/position?subject_id=123&resource_id=r1&position=1000', None),
    ('/history/seen?subject_id=123&progress=100&total=1000', None),
    ('/analytics/operation?action=test&target=123', None),
    ('/history/delete/123', None), ('/watchlist/toggle?subject_id=123&active=1', None),
    ('/history/progress', {'subject_id': '123', 'progress_ms': 100, 'total_ms': 1000}),
    ('/post/like?post_id=post1', None), ('/post/create?subject_id=123&content=synthetic-test', None),
])
def test_external_writes_are_simulated_only(account, url, body):
    client, _, _ = account
    response = client.post(url, json=body) if body is not None else client.post(url)
    assert response.status_code == 200, response.text
    assert response.json().get('code', 0) in (0, 200)


def test_community_failure_is_explicit_not_false_http_success(account, monkeypatch):
    client, provider, _ = account
    monkeypatch.setattr(provider, 'request', lambda *a, **kw: {'code': 1, 'msg': 'Upstream failed'})
    assert client.get('/groups/interactive').status_code == 503


@pytest.mark.parametrize('path,expected', [('/rooms/recommend',503), ('/rooms/room1',503),
                                         ('/groups/trending',503), ('/post/count/123',502)])
def test_legacy_community_reads_do_not_hide_business_errors(account, monkeypatch, path, expected):
    client, provider, sid = account
    failure = lambda *args, **kwargs: {'code': 1, 'msg': 'Upstream failed'}
    monkeypatch.setattr(provider, 'request', failure)
    monkeypatch.setattr(server.sessions[sid]['content'], 'get_rooms', failure)
    monkeypatch.setattr(server.sessions[sid]['content'], 'get_room_detail', failure)
    response = client.get(path)
    assert response.status_code == expected
    assert any(word in response.json()['detail'] for word in ('fournisseur', 'provider'))


def test_native_community_adapter_uses_verified_group_contract():
    from moviebox_api.content import MovieBoxContent
    calls = []
    def native(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith('/trending-entrance'):
            assert method == 'POST' and kwargs['data'] == {}
            return {'code': 0, 'data': {'items': [ROOM]}}
        assert method == 'GET' and kwargs['params']['groupId'] == 'room1'
        return {'code': 0, 'data': ROOM}
    content = MovieBoxContent(SimpleNamespace(request=native))
    assert content.get_rooms()['data']['items'][0]['groupId'] == 'room1'
    assert content.get_room_detail('room1')['data']['name'] == 'Groupe de test'
    assert content.get_rooms(page=2)['data']['items'] == []
    assert [c[1] for c in calls] == ['/wefeed-mobile-bff/group/list/trending-entrance',
                                    '/wefeed-mobile-bff/group/get']


def test_disabled_debug_has_expected_status(account):
    assert account[0].get('/debug_history').status_code == 410


def test_local_status_and_edition_positive_contracts(account):
    client = account[0]
    status = client.get('/api/v1/recommender/status')
    assert status.status_code == 200 and status.json()['data']['total'] == 5000
    edition = client.get('/api/v1/reference/editions/EDT-00001')
    assert edition.status_code == 200 and edition.json()['data']['edition']['id'] == 'EDT-00001'


@pytest.mark.parametrize('path,body', [
    ('/request-otp', {'account': ''}), ('/request-otp', {'account': '   '}),
    ('/request-otp', {'account': 'test@example.invalid', 'type': 99}),
    ('/login', {'account': 'test@example.invalid', 'password': ''}),
    ('/register', {'account': 'test@example.invalid', 'password': 'test', 'otp': ''}),
])
def test_invalid_account_forms_are_rejected_before_external_calls(account, path, body):
    client, provider, _ = account
    assert client.post(path, json=body).status_code == 422
    assert provider.calls == []


@pytest.mark.parametrize('url', ['/history/seen', '/history/seen?subject_id=123&progress=-1',
                                  '/history/position?subject_id=123&resource_id=r1&position=-1'])
def test_invalid_legacy_progress_never_reaches_provider(account, url):
    client, provider, _ = account
    assert client.post(url).status_code == 422
    assert provider.calls == []


def test_cloud_position_error_is_not_an_http_success(account, monkeypatch):
    client, provider, _ = account
    monkeypatch.setattr(provider, 'request', lambda *a, **kw: {'code': 400, 'msg': 'Invalid resource'})
    assert client.get('/history/position?subject_id=123&resource_id=0').status_code == 502


def test_legacy_suggestions_are_nonempty(account):
    assert account[0].get('/search-suggestions?q=Audit').json()['data'] == ['Audit Film']


def test_public_post_read_uses_correct_verb_id_and_zero_based_page(account):
    client, provider, _ = account
    response = client.get('/post/list/123?page=2')
    assert response.json()['data']['items'][0]['postId'] == 'post1'
    assert provider.calls[-1] == ('GET', '/wefeed-mobile-bff/post/list/subject',
                                   {'params': {'id': '123', 'page': 1, 'perPage': 10}})


def test_web_metadata_routes_and_feedback_success(account, monkeypatch):
    client, _, _ = account
    def upstream(method, route, **kwargs):
        if route == '/subject/trending':
            return {'code': 0, 'data': {'items': [SUBJECT]}}
        if route == '/subject/everyone-search':
            return {'code': 0, 'data': {'everyoneSearch': ['Audit Film']}}
        if route == '/tab-operating':
            return {'code': 0, 'data': {'operatingList': []}}
        if route == '/tab/get-bottom-tab-list':
            return {'code': 0, 'data': {'bottomTabs': [{'id': 2, 'title': 'Films'}]}}
        raise AssertionError(route)
    monkeypatch.setattr(web, 'upstream', upstream)
    for route in ('trending', 'popular-searches', 'tabs/2', 'navigation'):
        assert client.get('/api/v1/web/'+route).status_code == 200
    response = client.post('/api/v1/web/feedback', json={
        'subject_id': '123', 'category': 'other', 'comment': 'Retour synthétique de test'})
    assert response.status_code == 201, response.text
    assert len(client.get('/api/v1/web/feedback/123').json()['data']['items']) == 1


def test_genre_route_sends_current_native_genre(account, monkeypatch):
    client, _, _ = account
    calls = []
    original = web.live_filter
    def load(category, page, **kwargs):
        calls.append((category, kwargs['genre']))
        return original(category, page, **kwargs)
    monkeypatch.setattr(web, 'live_filter', load)
    data = client.get('/api/v1/genres/action').json()['data']
    assert data['items'] and data['source'] == 'web-bff-native-genre'
    assert calls == [('movies', 'Action'), ('series', 'Action')]


@pytest.mark.parametrize('failed', [('movies',), ('movies','series')])
def test_genre_keeps_available_category_and_reports_outage(account, monkeypatch, failed):
    from fastapi import HTTPException
    def load(category, page, **kwargs):
        if category in failed:
            raise HTTPException(502, 'Provider unavailable')
        return {'items': [SUBJECT], 'hasMore': False}
    monkeypatch.setattr(web, 'live_filter', load)
    response = account[0].get('/api/v1/genres/action')
    if len(failed) == 2:
        assert response.status_code == 502
        assert 'fournisseur' in response.json()['detail']
    else:
        assert response.status_code == 200
        data = response.json()['data']
        assert data['partial'] is True and data['unavailableCategories'] == ['movies']
        assert data['items'][0]['subjectId'] == '123'


@pytest.mark.parametrize('kind,expected', [(1, (0,0)), (2,(2,3))])
def test_subtitle_coordinates_follow_movie_or_episode(account, monkeypatch, kind, expected):
    client, _, _ = account
    calls = []
    monkeypatch.setattr(cine.engine, 'get_subtitles', lambda sid, **kw: calls.append(kw) or [])
    response = client.get('/api/v1/cinekivu/titles/123/subtitles', params={
        'season': 2, 'episode': 3, 'subject_type': kind})
    assert response.status_code == 200
    assert (calls[0]['season'],calls[0]['episode']) == expected


def test_legacy_download_and_playback_use_range_and_close_transport(account, setup, monkeypatch):
    import httpx
    from cinekivu_source import delivery
    client, _, _ = account
    seen = []
    def cdn(request):
        seen.append(request)
        assert request.headers['range'] == 'bytes=0-7'
        return httpx.Response(206, stream=httpx.ByteStream(b'12345678'), headers={
            'content-type': 'video/mp4', 'content-length': '8', 'content-range': 'bytes 0-7/1024'})
    with client as active:
        transport = httpx.AsyncClient(transport=httpx.MockTransport(cdn))
        monkeypatch.setattr(server.app.state, 'cdn_client', transport, raising=False)
        response = active.get('/download/movie?quality=720p&title=Audit', headers={'Range': 'bytes=0-7'})
        assert response.status_code == 206 and response.content == b'12345678'
        assert 'Audit' in response.headers['content-disposition']
        url = delivery._register_mp4_playback({'format': 'MP4', 'url': 'https://cdn.example.org/a.mp4'})
        play = active.get(url, headers={'Range': 'bytes=0-7'})
        assert play.status_code == 206 and play.content == b'12345678'
        active.portal.call(transport.aclose)
    assert len(seen) == 2


def test_caption_and_adaptive_manifest_routes(account, setup, monkeypatch):
    import cinekivu_bridge as bridge
    from cinekivu_source import delivery
    client, _, _ = account
    async def read(token):
        return b'1\n00:00:01,000 --> 00:00:02,000\nAudit\n'
    monkeypatch.setattr(bridge, 'read_caption', read)
    result = bridge._register_captions([{'url': 'https://cdn.example.org/fr.srt', 'code': 'fr'}])
    response = client.get(result[0]['url'])
    assert response.status_code == 200 and response.content.startswith(b'WEBVTT')
    presentation = SimpleNamespace(dash=b'<MPD/>', playlists={'master.m3u8': b'#EXTM3U\n'}, assets={})
    with delivery._playback_lock:
        import time
        delivery._adaptive_sources['audit-adaptive'] = {'presentation': presentation, 'headers': {}, 'expires': time.monotonic()+60}
    assert client.get('/api/adaptive/audit-adaptive/manifest.mpd').content == b'<MPD/>'
    assert client.get('/api/adaptive/audit-adaptive/master.m3u8').content.startswith(b'#EXTM3U')


def test_legacy_stream_recovers_with_verified_engine(account, setup, monkeypatch):
    from fastapi import HTTPException
    client = account[0]
    def unavailable(*args, **kwargs):
        raise HTTPException(502, 'Legacy source unavailable')
    monkeypatch.setattr(server, '_get_stream_raw', unavailable)
    response = client.get('/stream/movie?quality=720p')
    assert response.status_code == 200
    data = response.json()
    assert data['source'] == 'cinekivu-verified-relay-fallback' and data['url'].startswith('/api/play/')
    assert 'cdn.example.org' not in str(data) and data['cookie'] is None


def test_optional_captions_do_not_block_verified_legacy_video(account, setup, monkeypatch):
    from fastapi import HTTPException
    def unavailable(*args, **kwargs):
        raise HTTPException(502, 'Unavailable')
    monkeypatch.setattr(server, '_get_stream_raw', unavailable)
    monkeypatch.setattr(cine.engine, 'get_subtitles', unavailable)
    response = account[0].get('/stream/movie')
    assert response.status_code == 200 and response.json()['subtitlesPartial'] is True


@pytest.mark.parametrize('status', [401, 403, 429])
def test_legacy_stream_does_not_hide_provider_access_denials(account, monkeypatch, status):
    from fastapi import HTTPException
    def denied(*args, **kwargs):
        raise HTTPException(status, 'Provider refusal')
    monkeypatch.setattr(server, '_get_stream_raw', denied)
    assert account[0].get('/stream/123').status_code == status
