"""CineKivu Web BFF adapter using current provider responses only.

The home, catalogue, facets and discovery routes never read a bundled catalogue.
No stream authentication bypass.
"""
from __future__ import annotations

import httpx
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Query
from pydantic import BaseModel, Field

from cinekivu_source import api as cine
from cinekivu_metadata_cache import MetadataCache
from cinekivu_bridge import _subject, language_rank, valid_id
from cinekivu_native_catalogue import (SORT_IDS, LANGUAGE_IDS,
    native_filter_config, native_value, filter_options as native_options)
from cinekivu_rankings import TOP_FILMS_RANKING_ID, WEB_RANKING_USER_AGENT, resolve_ranking_navigation

log = logging.getLogger(__name__)
API_ROOT = 'https://h5-api.aoneroom.com/wefeed-h5api-bff'
RATING_ID = re.compile(r'^\d{1,21}$')
_catalogue_cache = MetadataCache(ttl_seconds=30, max_entries=128)


def upstream(method: str, route: str, *, params: dict | None = None, body: dict | None = None):
    """Fixed-route outbound requests: never accept an arbitrary URL from clients."""
    allow_get = {'/home', '/subject/trending', '/subject/everyone-search',
                 '/subject/detail-rec', '/ranking-list/content', '/tab-operating',
                 '/tab/get-bottom-tab-list', '/staff/subject-list',
                 '/staff/staff-related', '/post/list/subject', '/platform/play-list'}
    allow_post = {'/subject/filter', '/subject/search-suggest', '/subject/search'}
    if route not in (allow_get if method == 'GET' else allow_post):
        raise HTTPException(422, 'Route externe non autorisée')
    headers = {'User-Agent': WEB_RANKING_USER_AGENT if route == '/ranking-list/content' else cine.WEB_USER_AGENT,
               'Referer': 'https://movieboxhd.net/fr/',
               'Accept': 'application/json',
               'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
               'X-Request-Lang': 'fr'}
    try:
        # requests.Session from the existing CineKivu integration, no token or cookie spoofing
        extra = {'follow_redirects': False} if isinstance(cine.engine.session, httpx.Client) else {'allow_redirects': False}
        response = cine.engine.session.request(
            method, API_ROOT + route, params=params, json=body,
            headers=headers, timeout=11, **extra)
        if response.status_code != 200:
            if 400 <= response.status_code < 500:
                raise HTTPException(response.status_code, 'Requête rejetée par le fournisseur')
            raise ValueError(f'http_{response.status_code}')
        parsed = response.json()
        if not isinstance(parsed, dict) or parsed.get('code') not in (0, '0') or not isinstance(parsed.get('data'), (dict, list, str, type(None))):
            raise ValueError('invalid_provider_envelope')
        return parsed
    except HTTPException:
        # Never display stale data when the provider rejects authorization or rate limits.
        raise
    except Exception as e:
        log.info('Web BFF %s not available: %s', route, type(e).__name__)
        raise HTTPException(502, 'Le fournisseur Web ne répond pas ou refuse cette requête') from e


def normalize_title(raw: dict | None, banner_url: str = '') -> dict | None:
    if not isinstance(raw, dict):
        return None
    val = _subject(raw)
    if not val:
        return None
    src = raw.get('subject') if isinstance(raw.get('subject'), dict) else raw
    cover = (src.get('cover') or {}) if isinstance(src.get('cover'), dict) else {}
    trailer = src.get('trailer') if isinstance(src.get('trailer'), dict) else {}
    # Provider metadata may unexpectedly return genre/country as arrays or objects.
    def safe_text(value, limit=500):
        if isinstance(value, str): return value[:limit]
        if isinstance(value, list): return ', '.join(str(v) for v in value[:15] if isinstance(v, (str, int)))[:limit]
        if isinstance(value, (int, float)): return str(value)[:limit]
        return ''
    return {**val, 'genre': safe_text(src.get('genre')),
            'country': safe_text(src.get('countryName') or src.get('country')),
            'hasResource': bool(src.get('hasResource')), 'duration': src.get('duration') or None,
            'ratingCount': src.get('imdbRatingCount') or 0,
            'releaseDate': str(src.get('releaseDate') or src.get('release_date') or ''),
            'detailPath': str(src.get('detailPath') or src.get('detail_path') or raw.get('detailPath') or ''),
            'backdrop': banner_url or (src.get('stills') or {}).get('url', '') if isinstance(src.get('stills'), dict) else banner_url,
            'trailerAvailable': bool(trailer.get('videoAddress')),
            'audioVerified': False, 'subtitlesVerified': False}


def _dedupe(items: list[dict], max_items=100) -> list[dict]:
    seen: dict[str, dict] = {}
    for row in items:
        mapped = normalize_title(row) if isinstance(row, dict) else None
        if mapped and mapped['subjectType'] in (1, 2):
            seen.setdefault(mapped['subjectId'], mapped)
        if len(seen) >= max_items:
            break
    return sorted(seen.values(), key=language_rank)


def normalize_home(payload: dict):
    data = payload.get('data') or {}
    rows = data.get('operatingList') if isinstance(data, dict) else []
    if not isinstance(rows, list): rows = []
    result, banners = [], []
    for pos, block in enumerate(rows or []):
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        label = str(block.get('title') or 'À découvrir')[:120]
        if kind == 'BANNER':
            banner = block.get('banner')
            banner_items = banner.get('items') if isinstance(banner, dict) else []
            for item in banner_items if isinstance(banner_items, list) else []:
                if not isinstance(item, dict) or not isinstance(item.get('subject'), dict):
                    continue  # Ad banners without titles are not movies.
                picture = item.get('image') or {}
                large = str(picture.get('url') or '') if isinstance(picture, dict) else ''
                title = normalize_title(item, banner_url=large)
                if title and title['subjectType'] in (1, 2):
                    banners.append(title)
            continue
        if kind != 'SUBJECTS_MOVIE':
            continue
        raw_subjects = block.get('subjects')
        titles = ordered_trending({'data': {'items': raw_subjects if isinstance(raw_subjects, list) else []}})
        if titles:
            result.append({'id': str(block.get('opId') or f'rail-{pos}'),
                           'title': label, 'type': kind,
                           'position': block.get('position') or pos,
                           'items': titles, 'count': len(titles),
                           'rankingId': str(block.get('genreTopId') or '') if RATING_ID.fullmatch(str(block.get('genreTopId') or '')) else None,
                           'rankingPath': str(block.get('detailPath') or '')})
    banners = list({t['subjectId']: t for t in banners}.values())
    featured = (banners or [t for group in result for t in group['items']])[:1]
    return {'featured': featured[0] if featured else None, 'banners': banners,
            'sections': result, 'platforms': [p.get('name') for p in (data.get('platformList') if isinstance(data, dict) and isinstance(data.get('platformList'), list) else []) if isinstance(p, dict)],
            'sectionCount': len(result), 'stale': bool(payload.get('_fallback_stale')),
            'source': 'web-bff-live', 'observedAt': None, 'preferredLanguages': ['fr','en'],
            'note': 'Les titres et badges déclarés ne prouvent ni l’audio ni la lecture vidéo.'}


def french_first_home(web: dict) -> dict:
    """Prioritize declared French editions within the current provider banners."""
    banners = sorted(web['banners'], key=language_rank)
    return {**web, 'banners': banners,
            'featured': banners[0] if banners else web['featured']}


def listing(payload: dict, *keys: str):
    data = payload.get('data') or {}
    if isinstance(data, list):
        return _dedupe(data)
    if isinstance(data, dict):
        for key in (*keys, 'items', 'subjectList', 'subjects', 'list'):
            if isinstance(data.get(key), list):
                return _dedupe(data[key])
    return []


def ordered_trending(payload: dict) -> list[dict]:
    """Normalize real trending API entries without destroying its provider-defined order."""
    data = payload.get('data') or {}
    rows = data if isinstance(data, list) else next((data[key] for key in
        ('items', 'subjects', 'list', 'subjectList') if isinstance(data.get(key), list)), [])
    result, seen = [], set()
    for row in rows:
        mapped = normalize_title(row)
        if mapped and mapped['subjectType'] in (1, 2) and mapped['subjectId'] not in seen:
            result.append(mapped)
            seen.add(mapped['subjectId'])
    return result


def pager(payload: dict):
    data = payload.get('data') or {}
    obj = data.get('pager') if isinstance(data, dict) else {}
    if not isinstance(obj, dict): obj = {}
    # Upstream totalCount may be 0 even when hasMore=true.
    return {'hasMore': bool(obj.get('hasMore')), 'nextPage': obj.get('nextPage'), 'page': obj.get('page')}


def live_catalogue() -> dict:
    """Collect genuine film and series IDs from current editorial endpoints."""
    feeds = []
    for route, params in (('/home', {'host': 'movieboxhd.net'}),
                          ('/tab-operating', {'tabId': 2, 'host': 'movieboxhd.net'}),
                          ('/tab-operating', {'tabId': 5, 'host': 'movieboxhd.net'})):
        try:
            feed = normalize_home(upstream('GET', route, params=params))
            if feed['banners'] or feed['sections']:
                feeds.append(feed)
        except HTTPException:
            continue
    if not feeds:
        raise HTTPException(502, 'Catalogue en direct indisponible')
    titles: dict[str, dict] = {}
    for feed in feeds:
        for item in feed['banners']:
            titles.setdefault(item['subjectId'], item)
        for section in feed['sections']:
            for item in section['items']:
                titles.setdefault(item['subjectId'], item)
    if not titles:
        raise HTTPException(502, 'Aucun film ou série dans les réponses du fournisseur')
    return {'items': list(titles.values()), 'feeds': feeds, 'partial': len(feeds) < 3}


def _native_page(category: str, page: int, *, genre='', year='', lang='', country='',
                 sort='for_you', rate=None) -> dict:
    config = native_filter_config(category)
    # Film page network requests use 28 entries after each filter change.
    body = {'channelId': config['channelId'], 'page': page,
            'perPage': 28 if category == 'movies' else 30}
    for dimension, value in (('genre', genre), ('country', country), ('year', year),
                             ('classify', LANGUAGE_IDS.get(lang, lang)),
                             ('sort', SORT_IDS.get(sort, sort))):
        resolved = native_value(config, dimension, value)
        if resolved:
            body[dimension] = resolved
    if rate:
        if len(rate) != 2 or not 0 <= rate[0] <= rate[1] <= 10:
            raise HTTPException(422, 'Plage de notes invalide')
        body['rate'] = [str(value) for value in rate]
    def load():
        raw = upstream('POST', '/subject/filter', body=body)
        data = raw.get('data') or {}
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise HTTPException(502, 'Réponse de catalogue native invalide')
        # Cache only normalized public metadata, without playback URLs or credentials.
        items = ordered_trending(raw)
        expected_type = {'movies': 1, 'series': 2}.get(category)
        if expected_type:
            items = [item for item in items if item['subjectType'] == expected_type]
        pagination = pager(raw)
        total = (data.get('pager') or {}).get('totalCount')
        # 1,000,000 is the provider's unknown-total sentinel, not a real title count.
        total = total if isinstance(total, int) and 0 <= total < 1_000_000 else None
        return {'items': items, 'page': page, 'perPage': body['perPage'], 'hasMore': pagination['hasMore'],
                'nextPage': pagination['nextPage'],
                'pager': {**pagination, 'perPage': body['perPage'], 'totalCount': total},
                'totalLiveItems': total, 'stale': False, 'partial': False,
                'source': 'web-bff-native-filter', 'channelId': body['channelId']}

    key = (category, json.dumps(body, sort_keys=True, separators=(',', ':')))
    return _catalogue_cache.get_or_load(key, load)


def native_search(keyword: str, page: int) -> dict:
    """The public Web search rejects tokens; use the existing native mobile client."""
    try:
        raw = cine.engine.api_request('POST', '/wefeed-mobile-bff/subject-api/search',
                                      body_dict={'keyword': keyword, 'page': page, 'perPage': 20})
        if not isinstance(raw, dict) or raw.get('code') not in (0, '0') or not isinstance(raw.get('data'), dict):
            raise ValueError('invalid_search_response')
        return raw
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'La recherche native du fournisseur est indisponible') from exc


def live_filter(kind: str, page: int, *, genre: str = '', year: str = '', lang: str = '',
                country: str = '', q: str = '', sort: str = 'for_you', rate=None) -> dict:
    if q.strip():
        raw = native_search(q.strip(), page)
        values = ordered_trending(raw)
        # Search is a separate provider endpoint. Facets apply to this search page
        # only; never replace a search failure with unrelated editorial titles.
        values = filter_ranked_items(values, category=kind, genre=genre, country=country,
                                     year=year, language=lang, sort=sort)
        if rate:
            if len(rate) != 2 or not 0 <= rate[0] <= rate[1] <= 10:
                raise HTTPException(422, 'Plage de notes invalide')
            values = [item for item in values if _rating_in_range(item, rate)]
        return {'items': values, 'page': page, 'hasMore': pager(raw)['hasMore'],
                'pager': pager(raw), 'source': 'web-bff-native-search', 'stale': False,
                'partial': bool(genre or year or lang or country or rate or kind != 'all' or sort not in ('for_you', 'ForYou')),
                'note': 'Les facettes de recherche portent sur la page renvoyée par le fournisseur.'}
    categories = ('movies', 'series') if kind == 'all' else (kind,)
    pages = [_native_page(category, page, genre=genre, year=year, lang=lang,
                         country=country, sort=sort, rate=rate) for category in categories]
    if len(pages) == 1:
        return pages[0]
    # There is no native global film/series channel: channel 0 includes music.
    items = list({item['subjectId']: item for result in pages for item in result['items']}.values())
    return {'items': items, 'page': page, 'hasMore': any(result['hasMore'] for result in pages),
            'totalLiveItems': None, 'stale': False, 'partial': True,
            'source': 'web-bff-native-filter',
            'note': 'Pages natives films et séries réunies ; ordre conservé dans chaque catégorie.'}


def _rating_in_range(item, rate):
    try:
        return rate[0] <= float(item.get('score', '')) <= rate[1]
    except (TypeError, ValueError):
        return False


def live_filter_options(category: str) -> dict:
    categories = ('movies', 'series') if category == 'all' else (category,)
    return native_options([native_filter_config(value) for value in categories])


def live_title(subject_id: str) -> dict | None:
    try:
        return normalize_title(cine.engine.get_details(valid_id(subject_id)))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'Fiche actuelle indisponible') from exc


def native_ranking(ranking_id: str, page: int = 1) -> dict:
    if not RATING_ID.fullmatch(ranking_id):
        raise HTTPException(422, 'Identifiant de classement invalide')
    raw = upstream('GET', '/ranking-list/content', params={'id': ranking_id, 'page': page, 'perPage': 20})
    data = raw.get('data') or {}
    rows = data.get('subjectList') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise HTTPException(502, 'Réponse de classement native invalide')
    items, seen = [], set()
    for position, row in enumerate(rows, start=(page - 1) * 20 + 1):
        item = normalize_title(row)
        if item and item['subjectType'] in (1, 2) and item['subjectId'] not in seen:
            items.append({**item, 'rank': position})
            seen.add(item['subjectId'])
    pagination = pager(raw)
    total = (data.get('pager') or {}).get('totalCount')
    total = total if isinstance(total, int) and 0 < total < 1_000_000 else None
    return {'id': ranking_id, 'rankingId': ranking_id, 'title': str(data.get('title') or 'Classement'),
            'items': items, 'page': page, 'perPage': 20, 'hasMore': pagination['hasMore'],
            'nextPage': pagination['nextPage'], 'pager': {**pagination, 'perPage': 20, 'totalCount': total},
            'totalLiveItems': total, 'partial': False, 'stale': False,
            'source': 'web-bff-native-ranking'}


class FilterRequest(BaseModel):
    category: str = Field('all', pattern='^(all|movies|series|anime)$')
    genre: str = Field('', max_length=60)
    country: str = Field('', max_length=60)
    year: str = Field('', pattern=r'^(|(?:19|20)\d{2}|(?:19|20)\d0s|Other)$')
    language: str = Field('', max_length=60)
    keyword: str = Field('', max_length=100)
    page: int = Field(1, ge=1, le=100_000)
    sort: str = Field('for_you', pattern='^(for_you|hottest|latest|rating|ForYou|Hottest|Latest|Rating)$')
    rate: list[float] = Field(default_factory=list, max_length=2)


class FeedbackCreate(BaseModel):
    subject_id: str = Field(min_length=1, max_length=100)
    category: str = Field(pattern='^(review|episode_missing|audio_language|subtitle_language|quality|trailer_instead|player_error|other)$')
    season: int = Field(1, ge=1, le=999)
    episode: int = Field(1, ge=1, le=9999)
    rating: int | None = Field(None, ge=1, le=5)
    comment: str = Field(min_length=6, max_length=1600)
    audio_language: str = Field('', max_length=32)
    subtitle_language: str = Field('', max_length=32)
    platform: str = Field('web', max_length=20)


class FeedbackStore:
    """Only local, per-guest feedback, not comments from MovieBox."""
    def __init__(self, filename: str | None = None):
        self.filename = filename or os.getenv('CINEKIVU_FEEDBACK_DB', 'data/cinekivu_feedback.sqlite3')
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with sqlite3.connect(self.filename) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS feedback
              (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, subject_id TEXT NOT NULL,
               category TEXT NOT NULL, season INTEGER, episode INTEGER, rating INTEGER, comment TEXT NOT NULL,
               audio_language TEXT, subtitle_language TEXT, platform TEXT, created_at INTEGER NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_feedback_subject ON feedback(subject_id,created_at)')

    def insert(self, owner: str, data: FeedbackCreate):
        values = data.model_dump()
        with self.lock, sqlite3.connect(self.filename) as conn:
            recent = conn.execute('SELECT COUNT(*) FROM feedback WHERE owner=? AND created_at>?',
                                  (owner, int(time.time())-3600)).fetchone()[0]
            if recent >= 8:
                raise HTTPException(429, 'Limite de signalements atteinte pour cette session')
            cur = conn.execute('''INSERT INTO feedback
               (owner,subject_id,category,season,episode,rating,comment,audio_language,subtitle_language,platform,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
               (owner, *(values[k] for k in ('subject_id','category','season','episode','rating','comment',
                                           'audio_language','subtitle_language','platform')), int(time.time())))
            return cur.lastrowid

    def own(self, owner: str, subject_id: str):
        with sqlite3.connect(self.filename) as conn:
            rows = conn.execute('''SELECT id,category,season,episode,rating,comment,created_at FROM feedback
                                   WHERE owner=? AND subject_id=? ORDER BY id DESC LIMIT 40''',
                                (owner,subject_id)).fetchall()
        return [dict(zip(('id','category','season','episode','rating','comment','created_at'), row)) for row in rows]


def filter_ranked_items(items: list[dict], *, category: str = 'all', genre: str = '',
                        country: str = '', year: str = '', language: str = '',
                        sort: str = 'for_you') -> list[dict]:
    """Filter ONLY the retrieved source selection, never silently swap to another catalogue.

    Missing metadata does not pass a requested facet; no translation into fictitious dub tracks.
    """
    sort = {value: key for key, value in SORT_IDS.items()}.get(sort, sort)
    if category not in ('all', 'movies', 'series', 'anime') or sort not in ('for_you','hottest','latest','rating'):
        raise HTTPException(422, 'Paramètre de classement invalide')
    if year and not re.fullmatch(r'(?:19|20)\d{2}|(?:19|20)\d0s|Other', year):
        raise HTTPException(422, 'Année invalide')
    config = native_filter_config(category if category != 'all' else 'series') if (genre or country or language) else None
    def labels(dimension, value):
        if not value or not config:
            return []
        canonical = native_value(config, dimension, value)
        return [str(entry.get(key, '')).casefold() for group in config['items']
                if group.get('filterType') == dimension for entry in group.get('filterVals') or []
                if entry.get('id') == canonical for key in ('id', 'name')]
    genres = labels('genre', genre)
    countries = labels('country', country)
    def matches(item):
        kind = item.get('subjectType')
        if category == 'movies' and kind != 1: return False
        if category == 'series' and kind != 2: return False
        if category == 'anime' and not any(term in f"{item.get('genre','')} {item.get('title','')}".casefold()
                                           for term in ('anime','animation')): return False
        if genre and not any(value in str(item.get('genre') or '').casefold() for value in genres): return False
        if country and not any(value in str(item.get('country') or '').casefold() for value in countries): return False
        release_year = str(item.get('releaseDate') or '')[:4]
        if year.endswith('s') and (not release_year.isdigit() or not int(year[:4]) <= int(release_year) <= int(year[:4]) + 9): return False
        if year == 'Other':
            # "Other" is provider-defined and cannot be reconstructed on a search/rail page.
            raise HTTPException(422, 'Le filtre Autre est disponible dans le catalogue natif')
        if year and not year.endswith('s') and year != 'Other' and release_year != year: return False
        if language:
            declared = str(item.get('language') or '')
            if not declared:
                # The provider's title edition marker is not evidence of a native audio track.
                declared = str(item.get('title') or '')
            native_language = LANGUAGE_IDS.get(language, language)
            if native_language not in ('French dub', 'English dub'):
                raise HTTPException(422, 'Cette langue se filtre dans le catalogue natif')
            variants = ('français', 'french', '[vf]', ' vf', ' fr') if native_language == 'French dub' else ('english', 'anglais', '[vo]', ' en')
            if not any(code in declared.casefold() for code in variants): return False
        return True
    selected = [item for item in items if matches(item)]
    if sort == 'rating':
        def rating(item):
            try: return float(item.get('score') or item.get('rating') or 0)
            except (TypeError, ValueError): return 0
        selected.sort(key=rating, reverse=True)
    elif sort == 'latest':
        selected.sort(key=lambda item: str(item.get('releaseDate') or ''), reverse=True)
    # 'hottest' keeps the original provider ranking because there is no verified independent popularity count.
    return selected


def install_web_routes(app, get_session):
    router = APIRouter(prefix='/api/v1/web', tags=['CineKivu Web BFF & catalogue'])
    feedback = FeedbackStore()


    @router.get('/home')
    def home(mode: str = Query('fr', pattern='^(fr|live)$')):
        live = normalize_home(upstream('GET', '/home', params={'host':'movieboxhd.net'}))
        if not live['sections'] and not live['banners']:
            raise HTTPException(502, 'Accueil sans films ou séries exploitables')
        return {'code':0, 'data':live if mode == 'live' else french_first_home(live)}

    @router.get('/titles/{subject_id}')
    def title_live(subject_id: str):
        item = live_title(subject_id)
        if not item:
            try:
                item = normalize_title(cine.engine.get_details(valid_id(subject_id)))
            except Exception:
                pass
        if not item:
            try:
                recs = upstream('GET', '/subject/detail-rec', params={'subjectId': valid_id(subject_id)})
                items = (recs.get('data', {}) or {}).get('items', []) if isinstance(recs, dict) else []
                for entry in items:
                    norm = normalize_title(entry)
                    if norm and str(norm.get('subjectId')) == str(subject_id):
                        item = norm
                        break
            except Exception:
                pass
        if not item:
            raise HTTPException(404, 'Titre introuvable')
        return {'code':0,'data':{**item, 'stale':False,
                'cast':[], 'dubs':[], 'seasons':[], 'source':'cinekivu-live',
                'playbackVerified':False,
                'note':'Métadonnées actuelles du fournisseur ; lecture et pistes audio non vérifiées.'}}

    @router.get('/home/rails/{rail_id}')
    def rail(rail_id: str, page: int = Query(1, ge=1, le=100),
             category: str = Query('all', pattern='^(all|movies|series|anime)$'),
             genre: str = Query('', max_length=60), country: str = Query('', max_length=60),
             year: str = Query('', max_length=5), language: str = Query('', max_length=60),
             sort: str = Query('for_you', pattern='^(for_you|hottest|latest|rating)$')):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', rail_id):
            raise HTTPException(422, 'Rail invalide')
        content = home()['data']
        entry = next((s for s in content['sections'] if s['id'] == rail_id), None)
        if entry is None:
            raise HTTPException(404, 'Rail introuvable')
        if entry.get('rankingId'):
            if genre or country or year or language or category != 'all' or sort != 'for_you':
                raise HTTPException(422, 'Utilisez les onglets natifs pour changer de classement')
            return {'code': 0, 'data': native_ranking(entry['rankingId'], page)}
        subset = filter_ranked_items(entry['items'], category=category, genre=genre,
                country=country, year=year, language=language, sort=sort)
        start = (page - 1) * 24
        return {'code':0,'data':{**entry, 'items':subset[start:start+24],
                'totalLiveItems':len(subset), 'page':page, 'hasMore':len(subset)>start+24,
                'stale':content['stale'], 'source':content['source'], 'partial':True,
                'note':'Filtrage limité aux titres de cette sélection actuelle, non au catalogue intégral.'}}

    @router.get('/trending')
    def trending(page: int = Query(1, ge=1, le=200), perPage: int = Query(20, ge=1, le=50)):
        raw = upstream('GET','/subject/trending',params={'page':page,'perPage':perPage})
        return {'code':0,'data':{'items':listing(raw), 'pager':pager(raw), 'source':'web-bff-live'}}

    @router.get('/popular-searches')
    def popular():
        raw = upstream('GET','/subject/everyone-search')
        data = raw.get('data') or {}
        return {'code':0,'data':{'terms':data.get('everyoneSearch') or [], 'groups':data.get('hot') or [],
                                  'source':'web-bff-live'}}

    @router.get('/recommendations/{subject_id}')
    def recommendations(subject_id: str, page: int = Query(1, ge=1, le=30)):
        sid = valid_id(subject_id)
        raw = upstream('GET','/subject/detail-rec',params={'subjectId':sid,'page':page,'perPage':20})
        values = [row for row in listing(raw) if row['subjectId'] != sid]
        return {'code':0,'data':{'items':values,'source':'web-bff-live', 'stale':False,'page':page}}

    @router.get('/rankings')
    def ranking_navigation(ranking_id: str = Query('', max_length=21), path: str = Query('', max_length=100)):
        return {'code': 0, 'data': resolve_ranking_navigation(ranking_id, path,
            lambda: upstream('GET', '/home', params={'host': 'movieboxhd.net'}))}

    @router.get('/rankings/{ranking_id}')
    def rankings(ranking_id: str, page: int = Query(1, ge=1, le=200)):
        return {'code': 0, 'data': native_ranking(ranking_id, page)}

    @router.get('/tabs/{tab_id}')
    def tabs(tab_id: int = 2):
        if not 0 <= tab_id <= 20: raise HTTPException(422,'Onglet non valide')
        raw = upstream('GET','/tab-operating',params={'tabId':tab_id,'host':'movieboxhd.net'})
        result = normalize_home({'data': {'operatingList':(raw.get('data') or {}).get('operatingList',[])}})
        return {'code':0,'data':result}

    @router.get('/navigation')
    def navigation():
        raw = upstream('GET','/tab/get-bottom-tab-list')
        data = raw.get('data') or {}
        return {'code':0,'data':{'tabs':data.get('bottomTabs') or [],'source':'web-bff-live'}}

    @router.post('/filter')
    def filter_titles(filters: FilterRequest):
        result = live_filter(filters.category, filters.page, genre=filters.genre,
                             year=filters.year, country=filters.country,
                             lang=filters.language, q=filters.keyword, sort=filters.sort, rate=filters.rate)
        return {'code': 0, 'data': result}

    @router.get('/filter/options')
    def filter_options(category: str = Query('all', pattern='^(all|movies|series|anime)$')):
        return {'code': 0, 'data': live_filter_options(category)}

    @router.get('/categories/{category}')
    def curated_category(category: str, page: int = Query(1, ge=1, le=100_000),
                         subject_category: str = Query('all', alias='category', pattern='^(all|movies|series|anime)$'),
                         genre: str = Query('', max_length=60), country: str = Query('', max_length=60),
                         year: str = Query('', max_length=5), language: str = Query('', max_length=60),
                         sort: str = Query('for_you', pattern='^(for_you|hottest|latest|rating)$')):
        if category not in ('new','top10','anime','africa','short-series','hollywood','nollywood','bollywood','latest','trending'):
            raise HTTPException(404,'Catégorie éditoriale inconnue')
        if category == 'top10':
            return {'code': 0, 'data': native_ranking(TOP_FILMS_RANKING_ID, page)}
        if category in ('new', 'latest', 'anime', 'hollywood', 'nollywood', 'bollywood'):
            native_category = 'anime' if category == 'anime' else 'movies'
            native_country = {'hollywood': 'United States', 'nollywood': 'Nigeria',
                              'bollywood': 'India'}.get(category, country)
            native_sort = 'latest' if category in ('new', 'latest') else sort
            result = live_filter(native_category, page, genre=genre, country=native_country,
                                 year=year, lang=language, sort=native_sort)
            return {'code': 0, 'data': result}
        if category == 'trending':
            trending = upstream('GET', '/subject/trending', params={'page': page, 'perPage': 30})
            values = [row for row in ordered_trending(trending) if row['subjectType'] == 1]
            filtered = filter_ranked_items(values, category=subject_category, genre=genre,
                         country=country, year=year, language=language, sort=sort)
            # This is one provider-ranked page. Do not invent a rank for missing upstream pages.
            return {'code':0, 'data':{'items':filtered, 'page':page,
                    'hasMore':pager(trending)['hasMore'], 'source':'web-bff-live-trending',
                    'totalLiveItems':len(filtered), 'stale':False,
                    'partial':bool(genre or country or year or language or sort != 'for_you'),
                    'note':'Filtres appliqués aux résultats de cette page du classement externe.'}}
        # These two collections are provider editorial rails, explicitly scoped.
        # No hand-maintained list of countries or titles stands in for an API category.
        catalogue = live_catalogue()
        needles = {'africa': ('africa', 'afrique', 'nollywood', 'sa drama'),
                   'short-series': ('short', 'courtes')}[category]
        selected = {}
        for feed in catalogue['feeds']:
            for rail in feed['sections']:
                if any(term in rail['title'].casefold() for term in needles):
                    for row in rail['items']:
                        selected.setdefault(row['subjectId'], row)
        values = filter_ranked_items(list(selected.values()), category=subject_category,
                                     genre=genre, country=country, year=year,
                                     language=language, sort=sort)
        start = (page-1)*30
        return {'code':0,'data':{'items':values[start:start+30],'page':page,
                'hasMore':len(values)>start+30,'stale':False,
                'source':'web-bff-live-rails', 'partial':True,
                'note':'Collection issue des rayons actuels ; le classement ne couvre pas tout le fournisseur.'}}

    @router.get('/snapshot/search')
    def snapshot_search(q: str = Query('',max_length=100), page: int = Query(1, ge=1,le=200)):
        # Compatibility for older APKs; this route no longer reads a snapshot.
        return {'code':0,'data':{**live_filter('all',page,q=q), 'deprecatedRoute':True}}

    @router.get('/feedback/{subject_id}')
    def feedback_own(subject_id: str, session_id: str | None = Cookie(None)):
        owner = get_session(session_id)['id']
        return {'code':0,'data':{'items':feedback.own(owner,valid_id(subject_id)), 'scope':'current-session',
                                  'note':'Avis et signalements personnels enregistrés par CineKivu.'}}

    @router.post('/feedback',status_code=201)
    def feedback_submit(item: FeedbackCreate, session_id: str | None = Cookie(None)):
        valid_id(item.subject_id)
        sid = get_session(session_id)['id']
        rid = feedback.insert(sid,item)
        return {'code':0,'data':{'id':rid,'saved':True,'scope':'current-session'}}

    app.include_router(router)
    return router
