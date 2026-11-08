"""The real API with deterministic HTTP/provider fixtures for frontend interaction tests.

Never imported by production. The MP4 is the repository's authorised demo file.
External account/OTP/post operations are recorded locally, never sent.
"""
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

import sdk_fixture_app as sdk
import moviebox_api_server as backend
import cinekivu_web as web
import cinekivu_enrichment as enrichment
from cinekivu_source import api as cine
from cinekivu_media_source import source_client

app = sdk.app
sdk.MEDIA = (Path(__file__).resolve().parents[2]/'web-release/demo/demo.mp4').read_bytes()
ROOTS = ['fixture-old', 'fixture-new', 'fixture-series']
auth = SimpleNamespace(is_guest_mode=True, is_logged_in=False, user_info={})
account_calls = []


def subject(sid):
    item = sdk.details(sid)
    return {'subjectId': sid, 'title': item['title'], 'subjectType': item['subject_type'],
            'releaseDate': item['release_date'], 'detailPath': sid, 'description': 'Synopsis de test HTTP',
            'genre': 'Action', 'language': 'French', 'imdbRatingValue': '8.1'}


def subjects():
    return [subject(sid) for sid in ROOTS]


class Client:
    def request(self, method, path, **kwargs):
        if path.endswith('/trending-entrance'):
            assert method == 'POST'
            return {'code': 0, 'data': {'items': [{'groupId': 'community1', 'name': 'Cinéphiles de test', 'description': 'Communauté synthétique', 'userCount': 12}]}}
        if path.endswith('/group/get'):
            assert method == 'GET'
            return {'code': 0, 'data': {'groupId': 'community1', 'name': 'Cinéphiles de test', 'description': 'Communauté synthétique', 'userCount': 12}}
        if path.endswith('/interactive/post/list'):
            return {'code': 1, 'msg': 'Unavailable fixture feed'}
        if path.endswith('/post/list/subject'):
            assert method == 'GET' and 'id' in kwargs['params']
            return {'code': 0, 'data': {'items': [{'postId': 'post1', 'content': 'Commentaire du fournisseur de test', 'user': {'nickname': 'Spectateur'}}]}}
        if path.endswith('/post/count/subject'):
            return {'code': 0, 'data': {'count': 1}}
        if path.endswith(('/post/like', '/post/create')):
            account_calls.append(path)
            return {'code': 0, 'data': {'recorded': True}}
        raise HTTPException(501, 'Unmodelled frontend fixture dependency')

    def login(self, account, password, auth_type):
        assert account.endswith('@example.invalid')
        account_calls.append('simulated-login')
        auth.is_guest_mode, auth.is_logged_in, auth.user_info = False, True, {'nickname': 'Compte de test'}
        return {'status': 'success'}

    def register(self, account, password, otp, auth_type):
        assert account.endswith('@example.invalid')
        account_calls.append('simulated-register')
        return {'status': 'success'}

    def request_otp(self, account, auth_type, kind):
        assert account.endswith('@example.invalid')
        account_calls.append('simulated-otp')
        return {'status': 'success'}

    def logout(self):
        auth.is_guest_mode, auth.is_logged_in, auth.user_info = True, False, {}
        return {'status': 'success'}


client = Client()


class Content:
    def get_movie_detail(self, sid): return {'code': 0, 'data': subject(sid)}
    def get_detail_recommendations(self, sid): return {'code': 0, 'data': {'items': subjects()}}
    def get_recommendations(self, sid): return self.get_detail_recommendations(sid)
    def get_related_playback(self, sid): return self.get_detail_recommendations(sid)
    def get_staff_titles(self, sid): return self.get_detail_recommendations(sid)
    def get_rooms(self, **kwargs): return client.request('POST', '/wefeed-mobile-bff/group/list/trending-entrance')
    def get_room_detail(self, sid): return client.request('GET', '/wefeed-mobile-bff/group/get')


def session(sid=None):
    if sdk._session_id not in backend.sessions:
        auth.is_guest_mode, auth.is_logged_in = True, False
    value = {'id': sdk._session_id, 'client': client, 'content': Content(), 'auth': auth, 'user': SimpleNamespace()}
    backend.sessions[sdk._session_id] = value
    return value


backend.get_session = session


def config(category):
    return {'channelId': {'movies': 1, 'series': 2, 'anime': 1006}[category],
            'items': [{'filterType': 'genre', 'filterVals': [{'id': 'Action', 'name': 'Action'}]},
                      {'filterType': 'sort', 'filterVals': [{'id': 'ForYou', 'name': 'Pour vous'}]}]}


def upstream(method, route, params=None, body=None):
    if route in ('/home', '/tab-operating'):
        return {'code': 0, 'data': {'operatingList': [
            {'type': 'SUBJECTS_MOVIE', 'opId': 'rail1', 'title': 'Sélection de test HTTP', 'subjects': subjects()}]}}
    if route == '/subject/filter':
        values = [s for s in subjects() if s['subjectType'] == (1 if body['channelId'] == 1 else 2)]
        return {'code': 0, 'data': {'items': values, 'pager': {'hasMore': False, 'page': body.get('page', 1)}}}
    raise HTTPException(501, 'Unmodelled Web BFF fixture dependency')


def native(method, path, **kwargs):
    if path.endswith('/search'):
        q = kwargs.get('body_dict', {}).get('keyword', '').casefold()
        return {'code': 0, 'data': {'items': [s for s in subjects() if q in s['title'].casefold()], 'pager': {'hasMore': False}}}
    raise HTTPException(501, 'Unmodelled native fixture dependency')


async def metadata(path):
    item = subject(path)
    return {**item, 'cast': [], 'rating': {'value': 8.1}, 'trailer': {}}


async def reviews(*args, **kwargs):
    return {'items': [], 'hasMore': False, 'nextCursor': None}


web.upstream = upstream
web.native_filter_config = config
cine.engine.api_request = native
source_client.detail = metadata
source_client.reviews = reviews
enrichment.fetch_metadata = lambda provider, kind, query, limit: {'code': 0, 'data': {
    'provider': provider, 'kind': kind, 'items': [{'id': 'external1', 'title': query+' · métadonnées de test', 'provider': provider, 'description': 'Résultat synthétique'}]}}
