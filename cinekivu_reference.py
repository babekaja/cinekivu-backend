"""Read-only archive catalogue from the V28 MovieBox V5/V6 reference.

Historical metadata is kept separate from live MovieBox availability.
"""
from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from cinekivu_recommendations import normalized

DATA = Path(__file__).resolve().parent / 'resources/reference'


@lru_cache(maxsize=1)
def catalogue():
    def read(name):
        return json.loads((DATA / name).read_text(encoding='utf-8'))
    try:
        classification = read('classification_oeuvres_v5.json')['works']
        works = read('oeuvres_normalisees.json')
        editions = read('editions_et_pistes.json')['editions']
        result = {'rows': classification, 'works': {w['id']: w for w in works['works']},
                  'lookup': {w['id']: w for w in classification},
                  'editions': {e['id']: e for e in editions},
                  'taxonomy': read('taxonomie_v5.json'), 'stats': read('statistiques_v5.json'),
                  'locales': read('langues_et_i18n.json'), 'sources': read('sources_connecteurs.json'),
                  'date': works['date']}
        for row in classification:
            if row['id'] not in result['works'] or any(e not in result['editions'] for e in row['editions']):
                raise ValueError('Broken reference IDs')
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, 'Catalogue de référence indisponible') from None


def response(**values):
    return {'code': 0, 'data': {'source': 'cinekivu-reference-v5', 'historical': True,
                               'sourceDate': catalogue()['date'],
                               'streamAvailability': 'unknown', **values}}


class Filters(BaseModel):
    q: str = Field('', max_length=120)
    category: str = Field('', max_length=80)
    type: str = Field('', max_length=80)
    genre: str = Field('', max_length=80)
    format: str = Field('', max_length=80)
    collection: str = Field('', max_length=120)
    original_language: str = Field('', max_length=32)
    edition_language: str = Field('', max_length=32)
    audio_language: str = Field('', max_length=32)
    subtitle_language: str = Field('', max_length=32)
    year_min: int | None = Field(None, ge=1800, le=2200)
    year_max: int | None = Field(None, ge=1800, le=2200)
    verified_audio: bool = False
    verified_subtitles: bool = False
    authorized_playback: bool = False
    locale: str = Field('fr', max_length=32)
    sort: Literal['title', 'year_desc', 'year_asc'] = 'title'
    page: int = Field(1, ge=1)
    page_size: int = Field(24, ge=1, le=100)


def project(row, locale):
    names = catalogue()['works'][row['id']].get('titres_localises', {})
    return {'id': row['id'], 'title': names.get(locale) or names.get(locale[:2]) or row['titre'],
            'canonicalTitle': row['titre'], 'categories': row['categories'],
            'genres': row['genres_normalises'], 'formats': row['formats_suggeres_par_source'],
            'year': row['annee_relevee'], 'originalLanguage': row['langue_originale'],
            'editionCount': len(row['editions']), 'audioVerified': row['audio_confirme'],
            'subtitlesVerified': row['sous_titres_confirmes']}


def search(filters: Filters):
    data = catalogue()
    if filters.year_min is not None and filters.year_max is not None and filters.year_min > filters.year_max:
        raise HTTPException(422, 'year_min doit être inférieur ou égal à year_max')
    for value, field in [(filters.category, 'categories'), (filters.genre, 'genres')]:
        if value and value not in {r['id'] for r in data['taxonomy'][field]}:
            raise HTTPException(422, f'Filtre {field} inconnu')

    def language_match(value, requested):
        return bool(value and (value == requested or value.startswith(requested + '-')))

    rows = []
    for row in data['rows']:
        if filters.type and filters.type != row['type_source_v4']:
            continue
        if any(value and value not in row[key] for value, key in [
                (filters.category, 'categories'), (filters.genre, 'genres_normalises'),
                (filters.format, 'formats_suggeres_par_source')]):
            continue
        if filters.collection and normalized(filters.collection) not in {normalized(v) for v in row['rubriques_moviebox']}:
            continue
        year = row['annee_relevee']
        if filters.year_min is not None and (year is None or year < filters.year_min):
            continue
        if filters.year_max is not None and (year is None or year > filters.year_max):
            continue
        if filters.original_language and not language_match(row['langue_originale'], filters.original_language):
            continue
        editions = [data['editions'][e] for e in row['editions']]
        if filters.edition_language and not any(language_match(e['langue_version_indiquee'], filters.edition_language) for e in editions):
            continue
        if any(requested and not any(t.get('verifiee') and language_match(t.get('langue'), requested)
                                     for e in editions for t in e[field])
               for requested, field in [(filters.audio_language, 'audio_tracks'),
                                        (filters.subtitle_language, 'subtitle_tracks')]):
            continue
        if filters.verified_audio and not row['audio_confirme']:
            continue
        if filters.verified_subtitles and not row['sous_titres_confirmes']:
            continue
        if filters.authorized_playback and not row['droits']['diffusion_autorisee']:
            continue
        names = list(data['works'][row['id']].get('titres_localises', {}).values())
        searchable = ' '.join([row['titre']] + names + row['genres_source'] + row['rubriques_moviebox'])
        if filters.q.strip() and normalized(filters.q.strip()) not in normalized(searchable):
            continue
        rows.append(row)
    if filters.sort == 'year_desc':
        rows.sort(key=lambda r: (-(r['annee_relevee'] or -1), normalized(r['titre'])))
    elif filters.sort == 'year_asc':
        rows.sort(key=lambda r: (r['annee_relevee'] or 9999, normalized(r['titre'])))
    else:
        rows.sort(key=lambda r: normalized(r['titre']))
    offset = (filters.page - 1) * filters.page_size
    return response(items=[project(r, filters.locale) for r in rows[offset:offset + filters.page_size]],
                    total=len(rows), page=filters.page, pageSize=filters.page_size,
                    hasMore=offset + filters.page_size < len(rows))


def install_reference_routes(app: FastAPI):
    router = APIRouter(prefix='/api/v1/reference', tags=['CineKivu / métadonnées de référence'])

    @router.get('/search')
    def search_route(filters: Filters = Query()):
        return search(filters)

    @router.get('/taxonomy')
    def taxonomy():
        return response(taxonomy=catalogue()['taxonomy'])

    @router.get('/stats')
    def stats():
        return response(stats=catalogue()['stats'])

    @router.get('/categories')
    def categories():
        return response(items=catalogue()['taxonomy']['categories'])

    @router.get('/genres')
    def genres():
        return response(items=catalogue()['taxonomy']['genres'])

    @router.get('/formats')
    def formats():
        return response(items={r['id']: r['formats_admis'] for r in catalogue()['taxonomy']['categories']})

    @router.get('/collections')
    def collections():
        counts = Counter(c for w in catalogue()['rows'] for c in w['rubriques_moviebox'])
        return response(items=[{'title': k, 'count': v} for k, v in sorted(counts.items())])

    @router.get('/languages')
    def languages():
        loc = catalogue()['locales']
        return response(languages=loc['langues'], profiles=loc['profils_preferences'])

    @router.get('/locales/{locale}')
    def locales(locale: str):
        data = catalogue()['locales']
        if locale not in data['profils_preferences']:
            raise HTTPException(404, 'Locale inconnue')
        return response(profile=data['profils_preferences'][locale],
                        i18n=data['traductions_ui'].get(locale.split('-')[0], {}))

    @router.get('/sources')
    def sources():
        return response(sources=catalogue()['sources'], references=catalogue()['taxonomy']['sources_reference'])

    def work(work_id):
        data = catalogue()
        if work_id not in data['lookup']:
            raise HTTPException(404, 'Œuvre de référence inconnue')
        return data['lookup'][work_id]

    @router.get('/works/{work_id}')
    def detail(work_id: str):
        return response(classification=work(work_id), work=catalogue()['works'][work_id])

    @router.get('/works/{work_id}/editions')
    def editions(work_id: str):
        return response(workId=work_id, items=[catalogue()['editions'][e] for e in work(work_id)['editions']])

    @router.get('/works/{work_id}/recommendations')
    def recommendations(work_id: str, limit: int = Query(12, ge=1, le=30)):
        seed = work(work_id)
        matches = [(len(set(seed['genres_normalises']) & set(r['genres_normalises'])), r)
                   for r in catalogue()['rows'] if r['id'] != work_id
                   and set(seed['categories']) & set(r['categories'])]
        matches.sort(key=lambda pair: (-pair[0], normalized(pair[1]['titre']), pair[1]['id']))
        return response(workId=work_id, method='shared-genres', items=[
            {**project(r, 'fr'), 'sharedGenres': n} for n, r in matches if n > 0][:limit])

    @router.get('/editions/{edition_id}')
    def edition(edition_id: str):
        item = catalogue()['editions'].get(edition_id)
        if item is None:
            raise HTTPException(404, 'Édition inconnue')
        return response(edition=item)

    @router.get('/editions/{edition_id}/{section}')
    def edition_section(edition_id: str, section: Literal['tracks', 'seasons', 'episodes', 'volumes', 'chapters', 'rights']):
        item = edition(edition_id)['data']['edition']
        keys = {'seasons': 'saisons', 'episodes': 'episodes', 'volumes': 'volumes',
                'chapters': 'chapitres', 'rights': 'droits'}
        value = ({'audio': item['audio_tracks'], 'subtitles': item['subtitle_tracks'],
                  'declaredLanguage': item['langue_version_indiquee']} if section == 'tracks'
                 else item[keys[section]])
        return response(editionId=edition_id, section=section, value=value)

    app.include_router(router)
