"""Offline/provider-mocked discovery tests. No fabricated MovieBox availability."""
from fastapi.testclient import TestClient
import cinekivu_discovery as discovery
from moviebox_api_server import app, sessions, _sign_session

client = TestClient(app)
SID = '00000000-0000-4000-8000-000000000090'


class FakeContent:
    def get_search_autocomplete(self, q):
        return {'code':0,'data':{'items':[
            {'keyword':'Naruto Shippuden'},
            {'keyword':'Naruto [Version Française]'},
            {'word':'Naruto'},
        ]}}
    def get_recommendations(self, sid):
        return {'code':0,'data':{'items':[
            {'subjectId':sid,'title':'Film actuel'},
            {'subjectId':'s2','title':'Autre série','subjectType':2,'language':'en'},
            {'subjectId':'s3','title':'Autre film VF','subjectType':1,'language':'fr'},
            {'subjectId':'s3','title':'Autre film VF','subjectType':1},
        ]}}
    def get_movie_detail(self, sid):
        return {'code':0,'data':{
            'subjectId':sid,'title':'Film actuel',
            'staffList':[{'staffId':'a123','name':'Actrice Exemple','role':'Protagoniste','avatarUrl':'https://example.org/photo.jpg'},
                         {'staffId':'d456','name':'Réalisateur Exemple','role':'Réalisation'}],
        }}
    def get_staff_titles(self, staff_id, page=1):
        if staff_id == 'a123':
            return {'code':0,'data':{'items':[
                {'subjectId':'s3','title':'Autre film VF','subjectType':1},
                {'subjectId':'s4','title':'Film commun','subjectType':1},
            ]}}
        return {'code':0,'data':{'items':[{'subjectId':'s4','title':'Film commun','subjectType':1},
                                         {'subjectId':'s5','title':'Encore un film','subjectType':1}]}}
    def get_detail_recommendations(self,sid):
        return {'code':0,'data':{'items':[]}}


sessions[SID] = {'id':SID,'content':FakeContent()}
auth_client = TestClient(app, cookies={'session_id':_sign_session(SID)})


def test_autocomplete_parses_web_shapes_and_prioritizes_french():
    raw = {'code':0,'data':{'items':[
        {'subject':{'subjectId':'fr1','title':'Naruto [Version Française]','subjectType':2}},
        {'word':'Naruto'},
        {'word':'Naruto'},
        {'subject':{'subjectId':'en1','title':'Naruto Shippuden','language':'en'}},
        {'word':'Bollywood Top 10'},
    ]}}
    result = discovery.normalize_suggestions(raw, 'Naruto')
    assert result[0]['title'] == 'Naruto [Version Française]'
    assert result[0]['audioVerified'] is False
    assert len(result) == 3
    assert all('Bollywood' not in r['title'] for r in result)


def test_autocomplete_uses_cinekivu_first_and_internal_fallback(monkeypatch):
    monkeypatch.setattr(discovery, 'cine_autocomplete', lambda term: [
        {'query': 'Naruto [Version Française]','title':'Naruto [Version Française]',
         'subjectId':'fr1','subjectType':2,'cover':'','kind':'title','edition':'fr','audioVerified':False}
    ])
    resp=auth_client.get('/api/v1/search/autocomplete',params={'q':'Naruto'})
    assert resp.status_code == 200
    data=resp.json()['data']
    assert data['items'][0]['title']=='Naruto [Version Française]'
    assert data['source']=='cinekivu+internal'
    assert len(data['items']) == 3
    assert all(x['audioVerified'] is False for x in data['items'])
    assert 'Naruto' in [x['query'] for x in data['items']]


def test_autocomplete_empty_query_never_calls_remote(monkeypatch):
    monkeypatch.setattr(discovery, 'cine_autocomplete', lambda _: (_ for _ in ()).throw(AssertionError('should not call')))
    response=auth_client.get('/api/v1/search/autocomplete',params={'q':'N'})
    assert response.json()['data']['items']==[]
    assert auth_client.get('/api/v1/search/autocomplete',params={'q':'x'*101}).status_code==422


def test_autocomplete_partial_when_providers_offline(monkeypatch):
    monkeypatch.setattr(discovery, 'cine_autocomplete', lambda _: (_ for _ in ()).throw(OSError('offline')))
    response=auth_client.get('/api/v1/search/autocomplete',params={'q':'absent-target'})
    assert response.status_code == 200
    assert response.json()['data']['partial'] is True
    assert response.json()['data']['items']==[]


def test_similar_includes_cinekivu_and_internal_and_cast(monkeypatch):
    monkeypatch.setattr(discovery, 'cine_similar', lambda sid: [
        {'subjectId':'s6','title':'Même univers','subjectType':2,'language':'French'},
        {'subjectId':'s6','title':'Même univers','subjectType':2},
    ])
    resp=auth_client.get('/api/v1/titles/m123/recommendations')
    assert resp.status_code==200
    data=resp.json()['data']
    assert data['cast'][0]['name']=='Actrice Exemple'
    rows=data['sections']
    assert [r['id'] for r in rows]==['similar','cast']
    assert [x['subjectId'] for x in rows[0]['items']]==['s6','s3','s2']
    assert {x['subjectId'] for x in rows[1]['items']}=={'s4','s5'}
    assert 'm123' not in str(rows)


def test_cast_filmography_does_not_need_movie_details():
    payload=auth_client.get('/api/v1/people/a123/titles').json()['data']
    assert payload['personId']=='a123'
    assert {x['subjectId'] for x in payload['items']}=={'s3','s4'}
    assert auth_client.get('/api/v1/people/invalid%20id/titles').status_code==422


def test_recommendations_no_fabricated_results_when_all_sources_fail(monkeypatch):
    class DownContent:
        def get_recommendations(self, *_): raise OSError('offline')
        def get_detail_recommendations(self, *_): raise OSError('offline')
        def get_related_playback(self, *_): raise OSError('offline')
        def get_movie_detail(self, *_): raise OSError('offline')
    fail_id = '00000000-0000-4000-8000-000000000091'
    sessions[fail_id]={'id':fail_id,'content':DownContent()}
    other = TestClient(app, cookies={'session_id':_sign_session(fail_id)})
    monkeypatch.setattr(discovery,'cine_similar',lambda _: (_ for _ in ()).throw(OSError('offline')))
    response=other.get('/api/v1/titles/offline123/recommendations')
    assert response.status_code==200
    data=response.json()['data']
    assert data['sections']==[]
    assert data['cast']==[]
    assert data['partial'] is True


def test_routes_exist_in_openapi():
    names=app.openapi()['paths']
    assert '/api/v1/search/autocomplete' in names
    assert '/api/v1/titles/{subject_id}/recommendations' in names
    assert '/api/v1/people/{person_id}/titles' in names


def test_web_suggestion_uses_real_cinekivu_endpoint_contract(monkeypatch):
    from cinekivu_source import api as cine
    captured={}
    class Reply:
        def raise_for_status(self): return None
        def json(self): return {'code':0,'data':{'items':[
            {'subject':{'subjectId':'real1','title':'Naruto [Version Française]','subjectType':2}}
        ]}}
    def post(url, **kwargs):
        captured.update({'url':url, **kwargs})
        return Reply()
    monkeypatch.setattr(cine.engine.session, 'post', post)
    suggestions=discovery.cine_autocomplete('Naruto')
    assert captured['url'].endswith('/subject/search-suggest')
    assert captured['json']=={'keyword':'Naruto','perPage':16}
    assert captured['headers']['x-request-lang']=='fr'
    assert suggestions[0]['subjectId']=='real1'


def test_cinekivu_native_cast_is_preserved_without_internal_service(monkeypatch):
    from cinekivu_source import api as cine
    monkeypatch.setattr(cine.engine, 'get_details', lambda sid: {
        'subject_id':sid,'title':'Titre CineKivu','subject_type':1,
        'staffList':[{'staffId':'actor123','name':'Personne Exemple','role':'Interprète'}],
    })
    response=client.get('/api/v1/cinekivu/titles/abc123')
    assert response.status_code==200
    actor=response.json()['data']['cast'][0]
    assert actor['id']=='actor123'
    assert actor['name']=='Personne Exemple'


def test_internal_fallback_defaults_are_fr_and_congo(monkeypatch):
    from moviebox_api.client import MovieBoxClient
    from moviebox_api.auth import MovieBoxAuth
    from moviebox_api.utils import get_default_client_info
    for key in ('MOVIEBOX_REGION','MOVIEBOX_MCC','MOVIEBOX_LANGUAGE','MOVIEBOX_TIMEZONE'):
        monkeypatch.delenv(key, raising=False)
    meta=get_default_client_info()
    assert (meta['region'],meta['system_language'],meta['sp_code'],meta['timezone'])==('CD','fr','630','Africa/Lubumbashi')
    headers=MovieBoxClient(MovieBoxAuth()).get_auth_headers('1234')
    assert headers['region']=='CD' and headers['lang']=='fr'
