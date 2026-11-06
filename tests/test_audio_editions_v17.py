"""All tests offline. Provider labels are not verification of playback audio."""
from fastapi.testclient import TestClient
from moviebox_api_server import app
from cinekivu_source import api as cine
from cinekivu_audio_editions import (strip_edition, language_code, edition_language,
                                     same_work, edition_queries, discovered_editions)

client = TestClient(app)


def test_strip_variants_without_collapsing_franchises():
    assert strip_edition('Naruto [Version Française]') == 'Naruto'
    assert strip_edition('Naruto [English Dub]') == 'Naruto'
    assert strip_edition('Naruto - Spanish Dub') == 'Naruto'
    assert strip_edition('Naruto Shippuden') == 'Naruto Shippuden'
    assert strip_edition('The French Connection') == 'The French Connection'


def test_language_labels_are_not_subtitles_or_audio_proof():
    assert edition_language('Naruto [Version Française]') == 'fr'
    assert edition_language('Naruto [version françaises]') == 'fr'
    assert edition_language('Naruto [Japanese Dub]') == 'ja'
    assert edition_language('Naruto [English Dub]') == 'en'
    assert edition_language('Naruto VOSTFR') is None
    assert language_code('French subtitles') is None
    assert edition_language('The French Connection') is None
    assert language_code('pt-BR') == 'pt'


def test_strict_work_identity_type_and_year():
    current = {'title':'Naruto', 'subjectType':2, 'releaseTime':'2026'}
    assert same_work(current, {'title':'Naruto [French Dub]', 'subjectType':2, 'releaseTime':'2026'})
    assert not same_work(current, {'title':'Naruto Shippuden', 'subjectType':2})
    assert not same_work(current, {'title':'Naruto [French Dub]', 'subjectType':1})
    assert not same_work(current, {'title':'Naruto [French Dub]', 'subjectType':2,'releaseTime':'1998'})


def test_queries_bounded_and_multilingual():
    probes = edition_queries('Naruto')
    assert probes[0] == 'Naruto'
    assert 'Naruto [Version Française]' in probes
    assert 'Naruto [Japanese Dub]' in probes
    assert 'Naruto [Spanish Dub]' in probes
    assert 'Naruto [Portuguese Dub]' in probes
    assert 'Naruto [version françaises]' in probes
    assert len(probes) <= 16


def test_discover_merge_linked_and_search_and_vostfr():
    current={'title':'Naruto','subjectType':2,'releaseTime':'2026'}
    rows=[{'subjectId':'fr','title':'Naruto [Version Française]','subjectType':2,'releaseTime':'2026'},
          {'subjectId':'ja','title':'Naruto [Japanese Dub]','subjectType':2,'releaseTime':'2026'},
          {'subjectId':'sub','title':'Naruto VOSTFR','subjectType':2,'releaseTime':'2026'},
          {'subjectId':'wrong','title':'Naruto Shippuden [English Dub]','subjectType':2},
          {'subjectId':'different','title':'Naruto [English Dub]','subjectType':1}]
    value=discovered_editions('original',current,[{'subject_id':'fr','lang_name':'Français'},
                                                 {'subject_id':'en','lang_name':'English'}],
                               lambda query: rows if query.endswith('[Version Française]') else [])
    assert {r['subjectId'] for r in value['items']} == {'fr','en','ja','sub'}
    assert next(r for r in value['items'] if r['subjectId']=='fr')['languageCode']=='fr'
    assert next(r for r in value['items'] if r['subjectId']=='ja')['languageCode']=='ja'
    assert next(r for r in value['items'] if r['subjectId']=='sub')['languageCode'] is None
    assert all(not r['audioVerified'] for r in value['items'])


def test_partial_when_provider_search_fails_preserves_linked():
    value=discovered_editions('original', {'title':'Naruto','subjectType':2},
                              [{'subject_id':'fr','language':'fr'}], lambda _: (_ for _ in ()).throw(TimeoutError()))
    assert value['partial']
    assert value['french_editions'][0]['subjectId']=='fr'
    assert value['failed_search_count'] > 0


def test_endpoint_exposes_multiple_declared_and_separately_indexed_dubs(monkeypatch):
    monkeypatch.setattr(cine.engine, 'get_details', lambda sid:{
        'subject_id':sid, 'title':'Naruto','subject_type':2,'release_date':'2026',
        'dubs':[{'subject_id':'french','lang_name':'Français','language':'fr'},
                {'subject_id':'hindi','lang_name':'Hindi','language':'hi'}]})
    def search(q,page=1,per_page=20):
        if q.endswith('[English Dub]'):
            return [{'subject_id':'english','title':'Naruto [English Dub]','subject_type':2,'release_date':'2026'}]
        if q.endswith('[Japanese Dub]'):
            return [{'subject_id':'japanese','title':'Naruto [Japanese Dub]','subject_type':2,'release_date':'2026'}]
        return []
    monkeypatch.setattr(cine.engine, 'search',search)
    response=client.get('/api/v1/cinekivu/titles/naruto/editions')
    assert response.status_code == 200, response.text
    data=response.json()['data']
    assert {r['subjectId'] for r in data['items']} == {'french','hindi','english','japanese'}
    assert [r['languageCode'] for r in data['french_editions']] == ['fr']
    assert data['audio_verified'] is False


def test_cache_is_bounded_and_does_not_leak_mutable_state():
    from cinekivu_audio_editions import EditionCache
    cache = EditionCache(capacity=2, ttl=300)
    first = {'items':[{'subjectId':'fr'}], 'partial':False}
    cache.put('one', first)
    first['items'][0]['subjectId'] = 'rewritten'
    assert cache.get('one')['items'][0]['subjectId'] == 'fr'
    returned = cache.get('one')
    returned['items'].clear()
    assert len(cache.get('one')['items']) == 1
    cache.put('two', {'items':[]})
    cache.put('three', {'items':[]})
    assert cache.get('one') is None


def test_hindi_suffix_preserves_other_editions_and_their_language():
    current = {'title': 'Naruto: Shippuden [Hindi]', 'subjectType': 2}
    assert strip_edition(current['title']) == 'Naruto: Shippuden'
    assert edition_language(current['title'], 'Japanese') == 'hi'
    result = discovered_editions('hindi', current, [
        {'subject_id': 'english', 'lang_name': 'English dub', 'lanCode': 'en'},
        {'subject_id': 'bengali', 'lang_name': 'Bengali dub', 'lanCode': 'bn'},
        {'subject_id': 'original', 'lang_name': 'Original Audio', 'lanCode': 'ja', 'original': True},
    ], lambda _: [{'subjectId': 'french', 'title': 'Naruto: Shippuden [Version française]', 'subjectType': 2}])
    rows = {row['subjectId']: row for row in result['items']}
    assert set(rows) == {'english', 'bengali', 'original', 'french'}
    assert rows['english']['languageCode'] == 'en'
    assert rows['bengali']['languageCode'] == 'bn'
    assert rows['original']['original'] is True
    assert rows['original']['languageCode'] == 'ja'
    assert rows['english']['title'] == 'Naruto: Shippuden'
    assert rows['french']['languageCode'] == 'fr'
    assert all(not row['audioVerified'] for row in rows.values())
