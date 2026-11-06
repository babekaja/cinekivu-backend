"""Network-free fixtures: CI never contacts MovieBox or any third-party CDN."""
import pytest
from cinekivu_source import api as cine


@pytest.fixture(autouse=True)
def fresh_metadata_caches(monkeypatch):
    import cinekivu_native_catalogue as native
    import cinekivu_web as web
    from cinekivu_metadata_cache import MetadataCache
    monkeypatch.setattr(native, '_filter_config_cache', MetadataCache(ttl_seconds=600, max_entries=3))
    monkeypatch.setattr(web, '_catalogue_cache', MetadataCache(ttl_seconds=30, max_entries=128))


class Response:
    status_code = 200
    def __init__(self, content): self.content = content
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError('upstream error')
    def json(self): return self.content


@pytest.fixture(autouse=True)
def stub_cinekivu_provider(monkeypatch):
    def get(url, **kwargs):
        return Response({'code':0, 'data':{'sections':[
            {'title':'Découvrir', 'items':[
                {'subjectId':'m123','title':'Film exemple','cover':{'url':'https://example.org/m.jpg'},'language':'French','subjectType':1},
                {'subjectId':'s123','title':'Série exemple','subjectType':2,'language':'English'},
                {'subjectId':'hi123','title':'Hindi example','subjectType':1,'language':'Hindi'}]}]}})
    def post(url, **kwargs):
        payload = kwargs.get('json') or {}
        kind = payload.get('tabId') or (5 if payload.get('channelId') == 2 else 2)
        return Response({'code':0,'data':{'items':[
            {'subjectId':'s123' if kind==5 else 'm123', 'title':'Série exemple' if kind==5 else 'Film exemple',
             'subjectType': 2 if kind==5 else 1, 'language':'fr'}]}})
    monkeypatch.setattr(cine.engine.session, 'request', lambda method, url, **kwargs: post(url, **kwargs) if method == 'POST' else get(url, **kwargs))
    monkeypatch.setattr(cine.engine.session,'get',get)
    monkeypatch.setattr(cine.engine.session,'post',post)
    monkeypatch.setattr(cine.engine, 'search', lambda term, page=1, per_page=20: [])


@pytest.fixture(autouse=True)
def native_schema(monkeypatch):
    """A small provider schema fixture, isolated from the live metadata caches."""
    import cinekivu_web
    def config(category):
        groups = {
            'genre': [('Comedy', 'Comédie'), ('Animation', 'Animation'), ('Drama', 'Drame'), ('Action', 'Action')],
            'country': [('France', 'France'), ('Ghana', 'Ghana'), ('Nigeria', 'Nigeria'), ('United States', 'États-Unis'), ('India', 'Inde')],
            'year': [('2026', '2026'), ('2025', '2025'), ('2024', '2024'), ('2010s', '2010s'), ('Other', 'Autre')],
            'classify': [('French dub', 'Doublage français'), ('English dub', 'Doublage anglais'), ('Hindi dub', 'Doublage hindi')],
            'sort': [('ForYou', 'Pour toi'), ('Hottest', 'Le plus chaud'), ('Latest', 'Dernier'), ('Rating', 'Notation')],
        }
        return {'channelId': {'movies': 1, 'series': 2, 'anime': 1006}[category],
                'items': [{'filterType': name, 'filterVals': [{'id': value, 'name': label} for value, label in values]}
                          for name, values in groups.items()],
                'mobileItems': [{'filterType': 'rate', 'rangeVals': {'minVal': '0', 'maxVal': '10'}}]}
    monkeypatch.setattr(cinekivu_web, 'native_filter_config', config)
    return config
