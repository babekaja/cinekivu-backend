"""CineKivu-first source adapters for the existing MovieBox Internal OTT application.

The original CineKivu request/stream implementations live in `cinekivu_source/`.
No fake language metadata, transcoding, or availability is added by this bridge.
"""
from __future__ import annotations

import copy
import html
import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from cinekivu_source import api as cine
from cinekivu_source import delivery
from moviebox_api.edition_search import is_french_edition, base_title, related_editions
from cinekivu_audio_editions import discovered_editions, EditionCache
from cinekivu_text import synopsis

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SEASON = 1
MAX_CAPTION = 2 * 1024 * 1024
_caption_lock = threading.RLock()
_caption_sources: dict[str, dict[str, Any]] = {}
_edition_cache = EditionCache(capacity=80, ttl=300)


def valid_id(subject_id: str):
    if not _ID.fullmatch(subject_id):
        raise HTTPException(422, "Identifiant du contenu invalide")
    return subject_id


def _remote_error(exc: Exception):
    if isinstance(exc, HTTPException):
        return exc
    log.warning("Source CineKivu indisponible (%s)", type(exc).__name__)
    return HTTPException(502, "La source CineKivu ne répond pas ou a renvoyé des données invalides")


def _image(value):
    if isinstance(value, dict):
        return str(value.get('url') or value.get('image') or '')
    return str(value or '')


def language_rank(data: dict) -> int:
    """Sort by *declared* language/edition only. Unknown != Hindi != English."""
    title = str(data.get('title') or '')
    if is_french_edition(title):
        return 0
    langs = data.get('language') or data.get('languages') or data.get('lanName') or ''
    if isinstance(langs, list):
        langs = ' '.join(str(lang) for lang in langs)
    value = str(langs).casefold()
    if re.search(r'(^|[^a-z])(french|fran[cç]ais|fr-fr|fr)([^a-z]|$)', value):
        return 0
    if re.search(r'(^|[^a-z])(english|anglais|en-us|en-gb|en)([^a-z]|$)', value):
        return 1
    if re.search(r'(^|[^a-z])(hindi|hi-in|hindî)([^a-z]|$)', value):
        return 3
    return 2


def _subject(raw: dict):
    src = raw.get('subject') if isinstance(raw.get('subject'), dict) else raw
    sid = str(src.get('subjectId') or src.get('subject_id') or '')
    title = src.get('title') or src.get('subjectName') or src.get('name')
    if not sid or sid == 'None' or not title:
        return None
    stype = src.get('subjectType') or src.get('subject_type') or 1
    try: stype = int(stype)
    except (ValueError, TypeError): stype = 1
    cover = _image(src.get('cover') or src.get('poster') or src.get('thumbnail'))
    release = str(src.get('releaseDate') or src.get('release_date') or '')
    score = src.get('imdbRatingValue') or src.get('imdb_rating') or src.get('score')
    return {'subjectId': sid, 'id': sid, 'title': str(title), 'cover': cover,
            'poster': cover, 'description': synopsis(*(src.get(key) for key in ('description', 'synopsis', 'overview', 'desc', 'introduction'))),
            'releaseTime': release[:4] if release else '', 'score': str(score or 'N/A'),
            'subjectType': stype, 'language': src.get('language') or '',
            'edition': 'fr' if is_french_edition(str(title)) else 'unknown',
            'audioVerified': False, 'detail_path': src.get('detailPath') or src.get('detail_path')}


def _titles(raw: Any, limit: int = 150) -> list[dict]:
    """Extract titles from flexible CineKivu home envelopes, never section containers as films."""
    result = {}
    def walk(value, level=0):
        if len(result) >= limit or level >= 9:
            return
        if isinstance(value, list):
            for item in value[:250]: walk(item, level+1)
        elif isinstance(value, dict):
            title = _subject(value)
            if title:
                result.setdefault(title['subjectId'], title)
            else:
                for k, v in value.items():
                    if k not in ('headers', 'cookie', 'config', 'pagination') and isinstance(v, (dict, list)):
                        walk(v, level+1)
    walk(raw)
    # Stable sort; only known language metadata has priority.
    return sorted(result.values(), key=language_rank)


def _section_name(raw: str) -> str:
    names = {'trending': 'Tendances', 'popular': 'Populaires', 'recommended': 'À découvrir',
             'movie': 'Films', 'movies': 'Films', 'tv': 'Séries', 'series': 'Séries',
             'new': 'Nouveautés', 'latest': 'Nouveautés', 'top': 'Les mieux notés',
             'for you': 'Pour vous', 'hot': 'À la une'}
    key = str(raw or '').lower().strip()
    for needle, translation in names.items():
        if needle in key: return translation
    return str(raw or 'Découvrir')[:70]


def parse_home(raw: Any, page: int = 1) -> dict:
    value = raw.get('data', raw) if isinstance(raw, dict) else raw
    sections = []
    seen = set()
    def walk(node, level=0):
        if level > 6 or len(sections) >= 16:
            return
        if isinstance(node, dict):
            for key, entry in node.items():
                if key in ('subjects', 'items', 'list', 'rows', 'movieList', 'results', 'videos') and isinstance(entry, list):
                    titles = _titles(entry, 72)
                    if titles:
                        label = node.get('title') or node.get('name') or node.get('sectionName') or node.get('tabName') or key
                        ids = tuple(t['subjectId'] for t in titles)
                        if ids not in seen:
                            sections.append({'title': _section_name(str(label)), 'items': titles})
                            seen.add(ids)
                        continue
                if isinstance(entry, (list, dict)): walk(entry, level+1)
        elif isinstance(node, list):
            if any(isinstance(x, dict) and (x.get('subjectId') or isinstance(x.get('subject'), dict)) for x in node):
                titles = _titles(node, 72)
                ids = tuple(t['subjectId'] for t in titles)
                if titles and ids not in seen:
                    sections.append({'title': 'À découvrir', 'items': titles})
                    seen.add(ids)
            else:
                for item in node[:50]: walk(item, level+1)
    walk(value)
    if not sections:
        titles = _titles(value)
        if titles: sections.append({'title': 'À découvrir', 'items': titles})
    featured = next((t for sec in sections for t in sec['items'] if t['cover']), None)
    return {'featured': featured, 'sections': sections, 'page': page, 'source': 'cinekivu-home',
            'preferred_languages': ['fr', 'en'], 'audio_verified': False}


def cine_home(page: int = 1) -> dict:
    try:
        if page > 1:
            return {'featured': None, 'sections': [{'title': 'Films à découvrir', 'items': cine_catalog('movies', page)['items']}],
                    'page': page, 'source': 'cinekivu-filter', 'preferred_languages': ['fr', 'en']}
        response = cine.engine.session.get(f'{cine.API_BASE}/home?host=moviebox.ph',
                                           headers={'User-Agent': cine.WEB_USER_AGENT,
                                                    'Referer': 'https://moviebox.ph/'}, timeout=18)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict): raise ValueError('Home JSON shape')
        if data.get('code') not in (None, 0, 200, '0', '200'):
            raise ValueError('Upstream home returned an error')
        result = parse_home(data, page)
        if not result['sections']:
            raise ValueError('CineKivu home contains no recognizable titles')
        return result
    except Exception as exc:
        raise _remote_error(exc) from exc


def cine_catalog(category: str, page: int = 1) -> dict:
    if category not in ('movies', 'series', 'new', 'top10', 'home'):
        raise HTTPException(404, 'Catégorie inconnue dans CineKivu')
    if category == 'home':
        data = cine_home(page)
        return {'items': _titles([s['items'] for s in data['sections']]), 'page': page, 'category': category}
    from cinekivu_web import live_filter, native_ranking
    if category == 'top10':
        from cinekivu_rankings import TOP_FILMS_RANKING_ID
        return {**native_ranking(TOP_FILMS_RANKING_ID, page), 'category': category}
    native_category = 'series' if category == 'series' else 'movies'
    sort = 'latest' if category == 'new' else 'for_you'
    result = live_filter(native_category, page, sort=sort)
    return {**result, 'category': category, 'total': result.get('totalLiveItems')}


def cine_genre(genre_id: str, page: int = 1) -> dict:
    """Translate stable app genre IDs to each current provider filter schema."""
    from cinekivu_web import live_filter, native_filter_config
    aliases = {
        'action': ('action',), 'comedy': ('comedy', 'comedie'),
        'drama': ('drama', 'drame'), 'thriller': ('thriller',),
        'horror': ('horror', 'horreur'), 'romance': ('romance', 'romantique'),
        'animation': ('animation',), 'documentary': ('documentary', 'documentaire'),
        'science-fiction': ('sciencefiction', 'scifi'),
        'fantasy': ('fantasy', 'fantastique', 'fantaisie'),
    }
    def key(value):
        text = unicodedata.normalize('NFKD', str(value).casefold())
        return ''.join(c for c in text if c.isascii() and c.isalnum())
    values, pages, unavailable = {}, [], []
    for category in ('movies', 'series'):
        try:
            config = native_filter_config(category)
            group = next((g for g in config['items'] if g.get('filterType') == 'genre'), {})
            native = next((g['id'] for g in group.get('filterVals', [])
                           if any(key(g.get(field, '')) in aliases.get(genre_id, ())
                                  for field in ('id', 'name'))), None)
            if native is not None:
                data = live_filter(category, page, genre=native)
                pages.append(data)
                for item in data['items']:
                    values.setdefault(item['subjectId'], item)
        except HTTPException as exc:
            if exc.status_code < 500:
                raise
            unavailable.append(category)
    if not pages:
        if unavailable:
            raise HTTPException(502, 'Catalogue par genre momentanément indisponible chez le fournisseur')
        raise HTTPException(404, 'Genre actuellement absent des filtres du fournisseur')
    return {'items': list(values.values()), 'page': page, 'genre': genre_id,
            'hasMore': any(p.get('hasMore') for p in pages), 'partial': bool(unavailable),
            'unavailableCategories': unavailable,
            'source': 'web-bff-native-genre', 'stale': False}


def cine_search(q: str, page: int = 1, subject_type: int = 0):
    q = q.strip()
    candidates = [q]
    if not is_french_edition(q):
        candidates.extend([f'{base_title(q)} [Version Française]', f'{base_title(q)} VF'])
    found = {}
    partial = False
    started = time.monotonic()
    for term in dict.fromkeys(candidates):
        if time.monotonic() - started > 25:
            partial = True
            break
        try:
            for item in cine.engine.search(term, page=page):
                mapped = _subject(item)
                if mapped and (not subject_type or mapped['subjectType'] == subject_type):
                    found.setdefault(mapped['subjectId'], mapped)
        except Exception as exc:
            partial = True
            log.info('CineKivu search variant failed (%s)', type(exc).__name__)
    if not found and partial:
        raise HTTPException(502, 'Recherche CineKivu momentanément indisponible')
    values = sorted(found.values(), key=language_rank)
    return {'items': values, 'french_editions': [it for it in values if it['edition']=='fr'],
            'page': page, 'partial': partial, 'searched_queries': candidates,
            'edition_note': "Éditions identifiées par le titre ; piste audio non vérifiée.",
            'source': 'cinekivu-search', 'audio_verified': False}


def _caption_lang(source):
    lang = str(source.get('code') or source.get('language') or '').casefold().strip()
    if lang.startswith('fr') or 'french' in lang or 'français' in lang: return 'fr'
    if lang.startswith('en') or 'english' in lang or 'anglais' in lang: return 'en'
    return re.sub('[^a-z-]', '', lang)[:12] or 'und'


def _caption_rank(value):
    return {'fr': 0, 'en': 1}.get(value.get('lang'), 2)


def _register_captions(raw):
    results = []
    with _caption_lock:
        now = time.monotonic()
        for token in list(_caption_sources):
            if _caption_sources[token]['expires'] < now: del _caption_sources[token]
        for item in raw[:80]:
            url = item.get('url')
            if not delivery._public_https_media_url(url): continue
            if len(_caption_sources) > 500: del _caption_sources[next(iter(_caption_sources))]
            token = secrets.token_urlsafe(24)
            _caption_sources[token] = {'url': url, 'expires': now+1800}
            code = _caption_lang(item)
            results.append({'id': str(item.get('id') or token), 'language': item.get('language') or code,
                            'lang': code, 'url': f'/api/v1/cinekivu/captions/{token}.vtt',
                            'source': 'cinekivu', 'audioVerified': False})
    return sorted(results, key=_caption_rank)


def _vtt_timestamp(value):
    match = re.search(r'(?:(\d+):)?(\d{1,2}):(\d\d)[,.](\d{1,3})', value.strip())
    if not match: return None
    hours, minutes, seconds, ms = match.groups()
    return f'{int(hours or 0):02}:{int(minutes):02}:{seconds}.{ms.ljust(3,"0")}'


def convert_subtitle(raw: bytes):
    """Convert SRT/ASS (common upstream forms) to a minimal WebVTT file."""
    if len(raw) > MAX_CAPTION: raise ValueError('Fichier de sous-titres trop volumineux')
    text = raw.decode('utf-8-sig', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    if text.lstrip().startswith('WEBVTT'):
        return text.encode('utf-8')
    if '[Events]' in text and 'Dialogue:' in text:
        entries = []
        fields = ['layer','start','end','style','name','marginl','marginr','marginv','effect','text']
        in_events = False
        for line in text.splitlines():
            if line.startswith('['): in_events = line.strip().lower() == '[events]'
            if not in_events: continue
            if line.startswith('Format:'):
                fields = [s.strip().lower() for s in line.split(':',1)[1].split(',')]
            elif line.startswith('Dialogue:'):
                parts = line.split(':',1)[1].split(',', len(fields)-1)
                if len(parts) != len(fields): continue
                item = dict(zip(fields, parts))
                start, end = _vtt_timestamp(item.get('start','')), _vtt_timestamp(item.get('end',''))
                if not start or not end: continue
                caption = re.sub(r'\{[^}]*\}', '', item.get('text','')).replace('\\N','\n').replace('\\n','\n')
                entries.append(f'{start} --> {end}\n{html.unescape(caption)}')
        if not entries: raise ValueError('Aucune légende ASS compatible')
        return ('WEBVTT\n\n' + '\n\n'.join(entries) + '\n').encode('utf-8')
    lines = text.splitlines()
    srt_lines = ['WEBVTT', '']
    has_timing = False
    for line in lines:
        if '-->' in line:
            left, right = line.split('-->', 1)
            st, en = _vtt_timestamp(left), _vtt_timestamp(right)
            if st and en:
                srt_lines.append(st + ' --> ' + en)
                has_timing = True
            else: srt_lines.append(line.replace(',', '.'))
        elif line.strip().isdigit() and (not srt_lines or srt_lines[-1] == ''):
            continue
        else: srt_lines.append(line)
    if not has_timing: raise ValueError('Format de sous-titres non compatible')
    return ('\n'.join(srt_lines) + '\n').encode('utf-8')


async def read_caption(token: str) -> bytes:
    """Fetch one registered, bounded caption source; shared by playback and downloads."""
    with _caption_lock:
        source = _caption_sources.get(token)
    if not source or source['expires'] < time.monotonic():
        raise HTTPException(404, 'Sous-titres expirés ; rechargez la liste.')
    if not delivery._public_https_media_url(source['url']):
        raise HTTPException(502, 'Origine des sous-titres non autorisée')
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            async with client.stream('GET', source['url']) as resp:
                if resp.status_code in (404, 410):
                    raise HTTPException(404, 'Fichier de sous-titres absent chez le fournisseur')
                if resp.status_code != 200:
                    raise HTTPException(502, 'Sous-titres indisponibles')
                if int(resp.headers.get('content-length', '0')) > MAX_CAPTION:
                    raise HTTPException(413, 'Sous-titres trop volumineux')
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_CAPTION:
                        raise HTTPException(413, 'Sous-titres trop volumineux')
        return bytes(chunks)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, 'Format ou serveur de sous-titres indisponible') from exc


def install_cinekivu_routes(app):
    router = APIRouter(prefix='/api/v1/cinekivu', tags=['CineKivu · sources et lecture'])

    @router.get('/health')
    def source_health():
        return {'code': 0, 'data': {'engine': 'CineKivu', 'provider_reachable': 'not-probed',
                                  'revision': delivery.API_REVISION, 'home': 'cinekivu-web',
                                  'languages_priority': ['fr','en']}}

    @router.get('/titles/{subject_id}')
    def title(subject_id: str):
        try:
            raw = cine.engine.get_details(valid_id(subject_id))
            data = _subject(raw) or {}
            from cinekivu_discovery import normalize_people
            return {'code': 0, 'data': {**data, 'description': synopsis(*(raw.get(key) for key in ('description', 'synopsis', 'overview', 'desc', 'introduction'))),
                    'cast': normalize_people(raw),
                    'runtime': raw.get('duration'), 'dubs': raw.get('dubs') or [],
                    'raw_dubs': raw.get('dubs') or [], 'source': 'cinekivu',
                    'audio_verified': False}}
        except Exception as exc:
            try:
                from cinekivu_web import live_title
                fallback_item = live_title(subject_id)
                if fallback_item:
                    return {'code': 0, 'data': {**fallback_item, 'source': 'cinekivu-fallback'}}
            except Exception:
                pass
            raise _remote_error(exc) from exc

    @router.get('/titles/{subject_id}/editions')
    def editions(subject_id: str):
        """All declared dub links plus bounded catalog discovery for other languages.

        The label is not evidence of a playable audio track. The endpoint can
        return a partial result when provider searches time out or fail.
        """
        try:
            valid_id(subject_id)
            cached = _edition_cache.get(subject_id)
            if cached is not None:
                return {'code': 0, 'data': cached}
            source = cine.engine.get_details(subject_id)
            current = _subject(source)
            if not current:
                raise HTTPException(404, 'Titre inconnu dans CineKivu')
            current['audioVerified'] = False
            def search(term: str):
                return [_subject(row) for row in cine.engine.search(term, page=1)
                        if isinstance(row, dict)]
            result = discovered_editions(subject_id, current, source.get('dubs') or [],
                                         lambda term: (row for row in search(term) if row))
            data = {'current': current, **result}
            _edition_cache.put(subject_id, data)
            return {'code': 0, 'data': data}
        except Exception as exc:
            raise _remote_error(exc) from exc

    @router.get('/titles/{subject_id}/episodes')
    def episodes(subject_id: str):
        try:
            values = cine.engine.get_episodes(valid_id(subject_id))
            return {'code': 0, 'data': {'seasons': values, 'source': 'cinekivu'}}
        except Exception as exc: raise _remote_error(exc) from exc

    @router.get('/titles/{subject_id}/subtitles')
    def subtitles(subject_id: str, season: int = Query(1, ge=1, le=999),
                  episode: int = Query(1, ge=1, le=9999),
                  subject_type: int | None = Query(None, ge=1, le=2)):
        try:
            valid_id(subject_id)
            kind = subject_type
            if kind is None:
                try:
                    detail = cine.engine.get_details(subject_id)
                    kind = detail.get('subject_type') or detail.get('subjectType')
                except Exception:
                    pass
            source_season, source_episode = (0, 0) if str(kind) == '1' else (season, episode)
            entries = cine.engine.get_subtitles(subject_id, season=source_season, episode=source_episode)
            # A failed/missing detail lookup must not silently query a film as episode 1.
            if not entries and str(kind) not in ('1', '2'):
                entries = cine.engine.get_subtitles(subject_id, season=0, episode=0)
                if entries:
                    source_season, source_episode = 0, 0
            return {'code':0, 'data': {'items': _register_captions(entries), 'source': 'cinekivu',
                                     'priority': ['fr','en'], 'season': source_season,
                                     'episode': source_episode}}
        except Exception as exc: raise _remote_error(exc) from exc

    @router.get('/captions/{token}.vtt')
    async def caption_file(token: str):
        try:
            converted = convert_subtitle(await read_caption(token))
            return Response(converted, media_type='text/vtt; charset=utf-8',
                            headers={'Cache-Control': 'private, max-age=300', 'X-Content-Type-Options':'nosniff'})
        except HTTPException: raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, 'Format ou serveur de sous-titres indisponible') from exc

    @router.get('/titles/{subject_id}/streams')
    def streams(subject_id: str, season: int = Query(1, ge=1, le=999),
                episode: int = Query(1, ge=1, le=9999),
                quality: str = Query('auto', pattern='^(auto|360p|480p|720p|1080p|2160p)$'),
                detail_path: str | None = Query(None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,319}$'),
                subject_type: int | None = Query(None, ge=1, le=2)):
        valid_id(subject_id)
        try:
            # CineKivu performs its own media verification before advertising a local playback URL.
            # MovieBox expects se=ep=0 for films; series keep selected episode.
            details = None
            # The catalogue already knows these values. A failed optional mobile
            # detail lookup must not turn a known film into season 1/episode 1.
            if not (detail_path and subject_type is not None):
                try:
                    details = cine.engine.get_details(subject_id)
                except Exception:
                    pass
            kind = (details or {}).get('subject_type') or (details or {}).get('subjectType') or subject_type
            is_movie = str(kind) == '1'
            upstream = cine.engine.get_streams(
                subject_id, season=0 if is_movie else season, episode=0 if is_movie else episode,
                detail_path=(details or {}).get('detail_path') or (details or {}).get('detailPath') or detail_path,
                prefer_web_mp4=True)
            entries = delivery.prepare_streams(upstream)
            playable = []
            for item in entries:
                url = item.get('play_url')
                if not url or not url.startswith('/api/'):
                    continue  # Never return provider cookies, CDN URLs or headers.
                playable.append({'url': url, 'format': item.get('format'), 'quality': item.get('quality'),
                                 'quality_value': item.get('quality_value') or 0, 'codec': item.get('codec'),
                                 'duration': item.get('duration_sec'), 'source': item.get('source'),
                                 'download': delivery.register_download(item, subject_id,
                                     0 if is_movie else season, 0 if is_movie else episode)})
            playable.sort(key=lambda s: (s['format'] == 'HLS', s['quality_value']), reverse=True)
            selected = None
            if quality != 'auto':
                rank = int(quality.rstrip('p'))
                selected = next((p for p in playable if p['quality_value'] == rank), None)
            selected = selected or next(iter(playable), None)
            # Enriched diagnostic: no false claim of playability for an upstream-only URL.
            return {'code':0, 'data': {'subject_id': subject_id, 'season': season, 'episode': episode,
                    'url': selected['url'] if selected else None, 'sources': playable,
                    'playable_count': len(playable), 'source': 'cinekivu',
                    'issues': list(dict.fromkeys(s.get('playback_issue') for s in entries if s.get('playback_issue')))[:10],
                    'audio_verified': False,
                    'message': None if selected else 'Aucune source complète vérifiée disponible pour cet épisode.'}}
        except Exception as exc: raise _remote_error(exc) from exc

    app.include_router(router)
    async def resumable_download(media_id: str, request: Request):
        return await delivery.stable_download_stream(media_id, request, cine.engine)
    app.add_api_route('/api/download/by-id/{media_id}', resumable_download, methods=['GET'], tags=['Téléchargements CineKivu'])
    app.add_api_route('/api/download/by-id/{media_id}', resumable_download, methods=['HEAD'], include_in_schema=False)
    app.add_api_route('/api/download/{token}/{media_id}', delivery.download_stream,
                      methods=['GET'], tags=['Téléchargements CineKivu'])
    app.add_api_route('/api/download/{token}/{media_id}', delivery.download_stream,
                      methods=['HEAD'], include_in_schema=False)
    # CineKivu playback relay uses server-registered opaque, expiring tokens only.
    app.add_api_route('/api/play/{token}', delivery.play_stream, methods=['GET'], tags=['Lecture CineKivu'])
    app.add_api_route('/api/play/{token}', delivery.play_stream, methods=['HEAD'], include_in_schema=False)
    app.add_api_route('/api/adaptive/{token}/{resource:path}', delivery.adaptive_media,
                      methods=['GET'], tags=['Lecture CineKivu'])
    app.add_api_route('/api/adaptive/{token}/{resource:path}', delivery.adaptive_media,
                      methods=['HEAD'], include_in_schema=False)
