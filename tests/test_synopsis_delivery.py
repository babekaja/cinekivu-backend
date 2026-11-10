import json

import pytest
from fastapi.testclient import TestClient

import cinekivu_media
from cinekivu_media_source import parse_public_detail
from cinekivu_source import api as cine
from cinekivu_text import synopsis
from moviebox_api_server import app


@pytest.mark.parametrize('kind', [1, 2])
@pytest.mark.parametrize('field', ['description', 'synopsis', 'overview', 'desc', 'introduction'])
def test_movie_and_series_detail_keep_upstream_synopsis(monkeypatch, kind, field):
    def native(*args, **kwargs):
        return {'code': 0, 'data': {'subjectId': '123', 'title': 'Titre de test',
                                  'subjectType': kind, 'description': '', field: 'Un résumé fourni par la source.'}}
    monkeypatch.setattr(cine.engine, 'api_request', native)
    response = TestClient(app).get('/api/v1/cinekivu/titles/123')
    assert response.status_code == 200
    assert response.json()['data']['description'] == 'Un résumé fourni par la source.'
    assert response.json()['data']['subjectType'] == kind


def test_public_ssr_detail_preserves_description_instead_of_dropping_it():
    data = [{'subject': 1, 'postList': 5}, {'subjectId': 2, 'title': 3, 'description': 4},
            '123', 'Un film', '<p>Résumé &amp; personnages.</p>', []]
    html = '<script id="__NUXT_DATA__">'+json.dumps(data)+'</script>'
    parsed = parse_public_detail(html)
    assert parsed['subjectId'] == '123'
    assert parsed['description'] == 'Résumé & personnages.'


def test_public_ssr_detail_decodes_real_audio_editions_and_ignores_unsafe_paths():
    data = [
        {'subject': 1, 'postList': 15},
        {'subjectId': 2, 'title': 3, 'subjectType': 4, 'releaseDate': 5,
         'detailPath': 6, 'description': 7, 'dubs': 8},
        '123', 'Une série', 2, '2026-01-01', 'serie-safe', 'Résumé.', [9, 12],
        {'subjectId': 10, 'lanName': 11, 'lanCode': 16, 'original': 17, 'type': 4, 'detailPath': 18},
        '456', 'French dub',
        {'subjectId': 13, 'lanName': 14, 'lanCode': 19, 'original': 17, 'type': 4, 'detailPath': 20},
        '789', 'Hindi dub', [], 'fr', False, 'serie-fr', 'hi', 'https://evil.test/x']
    html = '<script id="__NUXT_DATA__">'+json.dumps(data)+'</script>'
    parsed = parse_public_detail(html)
    assert parsed['subjectType'] == 2 and parsed['detailPath'] == 'serie-safe'
    assert [(row['subjectId'], row['lanCode']) for row in parsed['dubs']] == [('456', 'fr')]


def test_overview_exposes_the_secondary_synopsis_for_frontend_enrichment(monkeypatch):
    async def metadata(subject_id, live):
        return ({'subject_id': subject_id, 'title': 'Film', 'description': ''},
                {'subjectId': subject_id, 'title': 'Film', 'synopsis': 'Résumé enrichi.'})
    monkeypatch.setattr(cinekivu_media, '_metadata', metadata)
    response = TestClient(app).get('/api/v1/media/123/overview')
    assert response.status_code == 200
    assert response.json()['subjectId'] == '123'
    assert response.json()['description'] == 'Résumé enrichi.'


def test_synopsis_is_bounded_and_never_invented():
    assert synopsis(None, False, {}, '', '   ') == ''
    assert synopsis('x' * 9000) == 'x' * 8000
    assert synopsis('Résumé natif', 'Résumé secondaire') == 'Résumé natif'
