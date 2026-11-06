"""French editions are separately indexed items, never fabricated audio streams."""
from fastapi.testclient import TestClient
from moviebox_api.edition_search import (
    is_french_edition, base_title, parse_query, candidate_queries, title_key,
    related_editions, search_editions,
)
from moviebox_api.ott import _items
from moviebox_api_server import app, sessions, _sign_session, map_item


class Provider:
    def __init__(self):
        self.queries = []
        self.fail_markers = False

    def search(self, keyword, page=1, subject_type=0):
        self.queries.append((keyword, page, subject_type))
        if self.fail_markers and '[' in keyword:
            raise ConnectionError('intentional offline network issue')
        if page > 1:
            return {'code': 0, 'data': {'items': [{'subjectId': 'naruto-2', 'title': 'Naruto: autre volume', 'subjectType': 2}]}}
        if 'Version Française' in keyword or 'Version Françaises' in keyword:
            return {'code': 0, 'data': {'items': [
                {'subjectId': 'naruto-fr', 'title': 'Naruto [Version Française]', 'subjectType': 2, 'year': '2002', 'cover': 'https://example.org/naruto-fr.jpg'},
                {'subjectId': 'ship-fr', 'title': 'Naruto Shippuden [Version Française]', 'subjectType': 2, 'year': '2007'},
                {'subjectId': 'naruto-fr', 'title': 'Naruto [Version Française]', 'subjectType': 2},
            ]}}
        return {'code': 0, 'data': {'items': [
            {'subjectId': 'naruto-original', 'title': 'Naruto', 'subjectType': 2, 'year': '2002'},
            {'subjectId': 'ship-original', 'title': 'Naruto Shippuden', 'subjectType': 2, 'year': '2007'},
            {'subjectId': 'french-film', 'title': 'The French Connection', 'subjectType': 1}
        ]}}

    def get_movie_detail(self, subject_id):
        if subject_id == 'naruto-fr':
            return {'code': 0, 'data': {'subjectId': subject_id, 'title': 'Naruto [Version Française]', 'subjectType': 2, 'year': '2002'}}
        return {'code': 0, 'data': {'subjectId': subject_id, 'title': 'Naruto', 'subjectType': 2, 'year': '2002'}}


SID = '00000000-0000-4000-8000-000000000045'
provider = Provider()
sessions[SID] = {'id': SID, 'content': provider}
client = TestClient(app, cookies={'session_id': _sign_session(SID)})


def test_edition_parsing_preserves_franchise_subtitles_and_explicit_queries():
    assert base_title('Naruto[version françaises]') == 'Naruto'
    assert base_title('Naruto [Version Française]') == 'Naruto'
    assert base_title('Naruto Shippuden - VF') == 'Naruto Shippuden'
    assert parse_query('Naruto [Version Françaises]') == ('Naruto', True)
    assert parse_query('Naruto VF') == ('Naruto', True)
    assert not is_french_edition('The French Connection')
    assert not is_french_edition('Naruto VOSTFR')
    assert title_key('NARUTO (VF)') == title_key('Naruto')
    probes = candidate_queries('Naruto[version françaises]')
    assert probes[0] == 'Naruto[version françaises]'
    assert 'Naruto [Version Française]' in probes
    assert len(probes) <= 6
    assert len(candidate_queries('Naruto', 2)) == 1


def test_plain_search_discovers_french_as_separate_subject_and_deduplicates():
    provider.queries.clear()
    result = client.get('/api/v1/search', params={'q': 'Naruto'})
    assert result.status_code == 200
    data = result.json()['data']
    ids = [i['subjectId'] for i in data['items']]
    assert ids.count('naruto-fr') == 1
    assert 'naruto-original' in ids and 'naruto-fr' in ids
    assert data['french_editions'][0]['subjectId'] == 'naruto-fr'
    assert data['french_editions'][0]['edition'] == 'fr'
    assert data['french_editions'][0]['audioVerified'] is False
    original = next(i for i in data['items'] if i['subjectId']=='naruto-original')
    assert any(i['subjectId']=='naruto-fr' for i in original['otherEditions'])
    assert all(i['subjectId']!='ship-fr' for i in original['otherEditions'])
    assert len(provider.queries) >= 3
    assert len(provider.queries) <= 6


def test_explicit_french_request_ranks_and_filters_editions():
    data = client.get('/api/v1/search', params={'q': 'Naruto[version françaises]'}).json()['data']
    assert data['items'][0]['subjectId'] == 'naruto-fr'
    fr = client.get('/api/v1/search', params={'q': 'Naruto[version françaises]', 'edition': 'fr'}).json()['data']
    assert fr['items']
    assert all(i['edition']=='fr' for i in fr['items'])


def test_related_editions_not_confused_with_shippuden():
    result = client.get('/api/v1/titles/naruto-original/editions')
    assert result.status_code == 200
    data = result.json()['data']
    assert any(i['subjectId']=='naruto-fr' for i in data['items'])
    assert all(i['subjectId'] != 'ship-fr' for i in data['items'])
    assert data['audio_verified'] is False
    fr_detail = client.get('/api/v1/titles/naruto-fr/editions').json()['data']
    assert any(i['subjectId']=='naruto-original' for i in fr_detail['items'])


def test_second_page_does_not_expand_and_partial_provider_errors():
    provider.queries.clear()
    page2 = client.get('/api/v1/search', params={'q': 'Naruto', 'page': 2}).json()['data']
    assert page2['items'][0]['subjectId']=='naruto-2'
    assert len(provider.queries) == 1
    provider.fail_markers = True
    partial = client.get('/api/v1/search', params={'q': 'OtherNarutoSpecial'}).json()['data']
    assert partial['partial'] is True
    assert partial['items']
    provider.fail_markers = False


def test_invalid_edition_filter_is_rejected():
    assert client.get('/api/v1/search', params={'q': 'Naruto', 'edition': 'audio_french_guaranteed'}).status_code == 422


def test_provider_indexes_only_compact_bracketed_spelling():
    class OnlyCompact:
        def __init__(self):
            self.probes=[]
        def search(self, keyword, page=1, subject_type=0):
            self.probes.append(keyword)
            if keyword.endswith('[version françaises]'):
                return {'code':0, 'data':{'items':[{'subjectId':'fr-only','title':'Naruto[version françaises]'}]}}
            return {'code':0,'data':{'items':[{'subjectId':'original-only','title':'Naruto'}]}}
    stub=OnlyCompact()
    data=search_editions(stub,map_item,_items,'Naruto')
    assert any(row['subjectId']=='fr-only' for row in data['french_editions'])
    assert 'Naruto[version françaises]' in stub.probes


def test_ott_search_caches_per_query_and_session():
    provider.queries.clear()
    keyword='Naruto cache test unique'
    initial=client.get('/api/v1/search',params={'q':keyword})
    assert initial.status_code==200
    before=len(provider.queries)
    repeat=client.get('/api/v1/search',params={'q':keyword,'edition':'fr'})
    assert repeat.status_code==200
    assert len(provider.queries)==before
