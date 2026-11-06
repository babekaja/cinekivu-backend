"""Offline contract checks. No requests to MovieBox or any CDN."""
import os
import tempfile
from pathlib import Path

# Set an isolated path before importing the app and its OTT router.
os.environ["MOVIEBOX_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test_ott.sqlite3")

from fastapi.testclient import TestClient
from moviebox_api_server import app, sessions, _sign_session
from moviebox_api.models import MovieDetail
from moviebox_api.ott import _items
from moviebox_api_server import map_item

class FakeContent:
    def get_categories(self, category_id=1, page=1):
        return {"code":0,"data":{"list":[{"name":"Sélection du moment","subjects":[{"subjectId":"m123","title":"Film exemple","cover":{"url":"https://example.org/m.jpg"},"subjectType":1}]}]}}
    def get_subject_list(self, page=1, category_type='1'):
        return {"code":0,"data":{"items":[{"subjectId":"m123" if category_type=='1' else "s123","title":"Film exemple" if category_type=='1' else "Série exemple"}]}}
    def get_discovery(self):
        return {"code":0,"data":{"items":[{"subjectId":"new123","title":"Nouveauté"}]}}
    def get_rankings(self, tab_id=1):
        return {"code":0,"data":{"subjects":[{"subjectId":"top123","title":"Top"}]}}
    def search(self, keyword, page=1, subject_type=0):
        return {"code":0,"data":{"items":[{"subjectId":"m123","title":"Film exemple"}]}}
    def get_recommendations(self, subject_id):
        return {"code":0,"data":{"list":[{"subjectId":"m123","title":"Film exemple"},{"subjectId":"r123","title":"Autre film"}]}}
    def get_related_playback(self, subject_id):
        return {"code":0,"data":{"items":[]}}
    def get_playlist_content(self, playlist_id, page=1):
        return {"code":0,"data":{"items":[{"subjectId":"m123","title":"Film exemple"}]}}
    def filter_items(self, filters, page=1):
        return {"code":0,"data":{"items":[{"subjectId":"m123","title":"Film exemple"}]}}
    def get_search_suggestions(self):
        return {"code":0,"data":{"list":[{"keyword":"Inception"}]}}

SID_A='00000000-0000-4000-8000-000000000001'
SID_B='00000000-0000-4000-8000-000000000002'
for sid in (SID_A,SID_B):
    sessions[sid]={"id":sid,"content":FakeContent()}

client_a=TestClient(app, cookies={'session_id':_sign_session(SID_A)})
client_b=TestClient(app, cookies={'session_id':_sign_session(SID_B)})


def test_route_registration_and_models():
    routes={(method.upper(), path) for path, operations in app.openapi()['paths'].items()
            for method in operations if method.lower() in {'get', 'post', 'put', 'patch', 'delete'}}
    assert ('GET','/api/v1/home') in routes
    assert ('PUT','/api/v1/progress/{subject_id}') in routes
    assert ('POST','/launch-player') in routes
    assert ('GET','/subtitles/search') in routes
    assert MovieDetail.__dataclass_fields__


def test_home_and_search_and_similar():
    h=client_a.get('/api/v1/home').json()
    assert h['code']==0
    assert h['data']['featured']['subjectId']=='m123'
    assert h['data']['sections'][0]['items'][0]['title']=='Film exemple'
    search=client_a.get('/api/v1/search',params={'q':'Film'}).json()
    assert search['data']['items'][0]['subjectId']=='m123'
    assert client_a.get('/api/v1/catalog/series').json()['data']['items'][0]['subjectId']=='s123'
    assert client_a.get('/api/v1/titles/m123/similar').json()['data']['items'][0]['subjectId']=='r123'
    assert client_a.get('/api/v1/collections/abc').json()['data']['items'][0]['subjectId']=='m123'
    assert client_a.get('/api/v1/genres/invalid').status_code==404


def test_public_feed_uses_last_cinekivu_success_when_provider_fails(monkeypatch):
    from cinekivu_source import api as cine
    class Flaky:
        failed=False
        def get(self, url, **kwargs):
            if self.failed: raise RuntimeError('network offline')
            return type('Response', (), {'status_code': 200, 'raise_for_status': lambda _:None,
                'json': lambda _: {'code':0,'data':{'items':[{'subjectId':'m123','title':'Film exemple','subjectType':1}]}}})()
    fake = Flaky()
    monkeypatch.setattr(cine.engine.session, 'get', fake.get)
    assert client_a.get('/api/v1/home').json()['data']['sections'][0]['items'][0]['subjectId'] == 'm123'
    fake.failed=True
    recovered = client_a.get('/api/v1/home').json()
    assert recovered['data']['stale'] is True
    assert recovered['meta']['source'] == 'cache'
    assert client_a.get('/api/v1/home',params={'page':92}).json()['data']['source'] == 'cinekivu-filter'


def test_local_library_and_no_guest_leakage():
    assert client_a.put('/api/v1/library/m123',json={'title':'Film exemple','cover':'https://example.org/m.jpg'}).status_code==200
    assert len(client_a.get('/api/v1/library').json()['data']['items'])==1
    assert len(client_b.get('/api/v1/library').json()['data']['items'])==0
    assert client_a.delete('/api/v1/library/m123').json()['data']['saved']==False
    assert client_a.get('/api/v1/library').json()['data']['items']==[]


def test_progress_continue_watching_and_preferences():
    payload={'title':'Série exemple','subject_type':2,'season':1,'episode':2,'position_seconds':90,'duration_seconds':120}
    assert client_a.put('/api/v1/progress/s123',json=payload).status_code==200
    assert client_a.get('/api/v1/progress/s123',params={'season':1,'episode':2}).json()['data']['position_seconds']==90
    cont=client_a.get('/api/v1/continue-watching').json()['data']['items']
    assert cont[0]['progress_ratio']==.75
    assert client_b.get('/api/v1/continue-watching').json()['data']['items']==[]
    assert client_a.patch('/api/v1/preferences',json={'language':'fr','subtitle_language':'off','autoplay':True}).json()['data']['autoplay']
    assert client_a.get('/api/v1/preferences').json()['data']['subtitle_language']=='off'
    assert client_a.patch('/api/v1/preferences',json={'language':'invalid'}).status_code==422
    assert client_a.delete('/api/v1/progress/s123').json()['data']['deleted']


def test_error_handling_and_deprecated_proxies():
    assert client_a.get('/api/v1/catalog/does-not-exist').status_code==404
    assert client_a.put('/api/v1/library/%20illegal',json={'title':'X'}).status_code==422
    assert client_a.post('/launch-player',params={'player':'mpv','url':'https://example.org'}).status_code==410
    assert client_a.get('/sub-proxy',params={'u':'http://127.0.0.1/'}).status_code==410


def test_nested_normalization():
    raw={'data':{'rows':[{'items':[{'subject':{'subjectId':'ok123','title':'Visible'}},{'foo':'no-title'},{'subjectId':'ok123','title':'Duplicate'}]}]}}
    assert len(_items(raw,map_item))==1
