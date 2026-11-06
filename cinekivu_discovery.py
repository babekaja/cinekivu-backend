"""CineKivu-first autocomplete, similar titles and cast filmography.

The remote provider is best-effort. Unknown upstream shapes or connectivity must
never turn into fabricated film suggestions or verified-dubbing claims.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import APIRouter, Cookie, FastAPI, HTTPException, Query
from moviebox_api.ott import _items
from cinekivu_bridge import _subject, language_rank, valid_id
from cinekivu_source import api as cine

log = logging.getLogger(__name__)
_BASE = cine.API_BASE
_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple, tuple[float, dict]] = {}
_MAX_CACHE = 200
_ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')


def _cache_get(key: tuple) -> dict | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return copy.deepcopy(hit[1])
        if hit:
            _CACHE.pop(key, None)
    return None


def _cache_put(key: tuple, value: dict, lifetime: int = 180) -> dict:
    with _CACHE_LOCK:
        if len(_CACHE) >= _MAX_CACHE:
            expired = [k for k, (t, _) in _CACHE.items() if t <= time.monotonic()]
            for k in expired:
                _CACHE.pop(k, None)
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic() + lifetime, copy.deepcopy(value))
    return value


def _walk(value: Any, depth: int = 0):
    """Look only inside known response envelope fields; cap total work."""
    if depth > 7:
        return
    if isinstance(value, list):
        for entry in value[:80]:
            yield from _walk(entry, depth + 1)
    elif isinstance(value, dict):
        if any(k in value for k in ('subjectId', 'subject_id', 'keyword', 'word', 'query')):
            yield value
        for field in ('data', 'items', 'list', 'subjects', 'results', 'suggestions',
                      'keywords', 'words', 'subject', 'recommendations', 'rows', 'sections', 'info'):
            nested = value.get(field)
            if isinstance(nested, (dict, list)):
                yield from _walk(nested, depth + 1)
    elif isinstance(value, str):
        yield {'keyword': value}


def normalize_suggestions(payload: Any, query: str, limit: int = 8) -> list[dict]:
    """Support title and keyword suggestions (with or without subject ids)."""
    query = query.strip().casefold()
    unique: dict[str, dict] = {}
    for raw in _walk(payload):
        sub = raw.get('subject') if isinstance(raw.get('subject'), dict) else raw
        title = sub.get('title') or sub.get('subjectName') or raw.get('word') or raw.get('keyword') or raw.get('query') or raw.get('name')
        if not isinstance(title, str):
            continue
        title = ' '.join(title.split())[:120]
        if not title or (query and query not in title.casefold() and not title.casefold().startswith(query.split(' ')[0])):
            # Provider sometimes returns trending rather than completions: don't mislabel them.
            continue
        sid = str(sub.get('subjectId') or sub.get('subject_id') or '')
        if not _ID.fullmatch(sid):
            sid = ''
        item = _subject(sub) if sid else None
        result = {'query': title, 'title': title, 'subjectId': sid or None,
                  'subjectType': item.get('subjectType') if item else None,
                  'cover': item.get('cover') if item else '',
                  'kind': 'title' if sid else 'query',
                  'edition': 'fr' if item and item.get('edition') == 'fr' else 'unknown',
                  'audioVerified': False}
        unique.setdefault(title.casefold(), result)
        if len(unique) >= 60:
            break
    return sorted(unique.values(), key=lambda i: (language_rank(i), 0 if i['kind'] == 'title' else 1, len(i['title'])))[:limit]


def _usable(raw: Any) -> Any:
    if not isinstance(raw, dict) or raw.get('code', 0) not in (0, 200, '0', '200', None):
        raise ValueError('invalid provider response')
    return raw


def cine_autocomplete(query: str):
    """Web endpoint already referenced in CineKivu source: POST /subject/search-suggest."""
    response = cine.engine.session.post(
        f'{_BASE}/subject/search-suggest',
        headers={'User-Agent': cine.WEB_USER_AGENT, 'Referer': 'https://moviebox.ph/',
                 'x-request-lang': 'fr', 'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7'},
        json={'keyword': query, 'perPage': 16}, timeout=7)
    response.raise_for_status()
    return normalize_suggestions(_usable(response.json()), query)


def cine_similar(subject_id: str) -> list[dict]:
    """Use only current Web BFF recommendations for this subject."""
    from cinekivu_web import upstream, listing
    raw = upstream('GET', '/subject/detail-rec', params={
        'subjectId': valid_id(subject_id), 'page': 1, 'perPage': 24})
    return _dedupe_titles(listing(raw), subject_id)


def _dedupe_titles(items: list[dict], subject_id: str, limit: int = 24) -> list[dict]:
    found: dict[str, dict] = {}
    for value in items:
        sid = str(value.get('subjectId') or '')
        if _ID.fullmatch(sid) and sid != subject_id and value.get('title') and value['title'] != 'Unknown':
            found.setdefault(sid, value)
    return sorted(found.values(), key=language_rank)[:limit]


def normalize_people(raw: Any, limit: int = 18) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    data = raw.get('data') if isinstance(raw.get('data'), dict) else raw
    actors = data.get('staffList') or data.get('actorList') or data.get('cast') or []
    if not isinstance(actors, list):
        return []
    people, seen = [], set()
    for p in actors[:limit * 2]:
        if not isinstance(p, dict):
            continue
        name = str(p.get('name') or p.get('staffName') or p.get('actorName') or '').strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        pid = str(p.get('staffId') or p.get('actorId') or p.get('id') or '')
        avatar = p.get('avatarUrl') or p.get('avatar') or p.get('photo') or p.get('cover') or ''
        if isinstance(avatar, dict):
            avatar = avatar.get('url') or ''
        avatar = str(avatar) if isinstance(avatar, str) else ''
        if avatar.startswith('//'):
            avatar = 'https:' + avatar
        people.append({'id': pid if _ID.fullmatch(pid) else None,
                       'name': name[:140], 'role': str(p.get('character') or p.get('role') or 'Interprète')[:140],
                       'avatar': avatar})
        if len(people) >= limit:
            break
    return people


def install_discovery_routes(app: FastAPI, get_session: Callable, mapper: Callable):
    router = APIRouter(prefix='/api/v1', tags=['CineKivu / découverte'])

    @router.get('/search/autocomplete')
    def autocomplete(q: str = Query('', max_length=100), limit: int = Query(8, ge=1, le=15),
                     session_id: str | None = Cookie(None)):
        term = ' '.join(q.split())
        if len(term) < 2:
            return {'code': 0, 'data': {'items': [], 'query': term, 'partial': False, 'source': 'cinekivu'}}
        key = ('autocomplete', term.casefold(), limit)
        cached = _cache_get(key)
        if cached is not None:
            return {'code': 0, 'data': cached}
        failed = False
        sources = []
        try:
            values = cine_autocomplete(term)
            sources.append('cinekivu')
        except Exception as exc:
            log.info('CineKivu autocomplete unavailable (%s)', type(exc).__name__)
            values, failed = [], True
        # Internal search-suggest is supplementary, not the home feed.
        if len(values) < limit:
            try:
                provider = get_session(session_id)['content']
                extra = normalize_suggestions(provider.get_search_autocomplete(term), term, 15)
                keys = {x['title'].casefold() for x in values}
                values += [x for x in extra if x['title'].casefold() not in keys]
                sources.append('internal')
            except Exception as exc:
                failed = True
                log.info('Internal autocomplete unavailable (%s)', type(exc).__name__)
        data = {'items': sorted(values, key=lambda i: (language_rank(i), i['kind'] != 'title'))[:limit],
                'query': term, 'partial': failed, 'source': '+'.join(sources) or 'unavailable'}
        if values:
            _cache_put(key, data, 120)
        return {'code': 0, 'data': data}

    @router.get('/titles/{subject_id}/recommendations')
    def recommendations(subject_id: str, session_id: str | None = Cookie(None)):
        sid = valid_id(subject_id)
        s = get_session(session_id)
        key = ('recommendations', s['id'], sid)
        cached = _cache_get(key)
        if cached is not None:
            return {'code': 0, 'data': cached}
        c = s['content']

        def internal_details():
            return _usable(c.get_movie_detail(sid))

        def internal_related():
            for fetch in (lambda: c.get_recommendations(sid),
                          lambda: c.get_detail_recommendations(sid),
                          lambda: c.get_related_playback(sid)):
                try:
                    raw = _usable(fetch())
                    titles = _dedupe_titles(_items(raw, mapper, 70), sid)
                    if titles:
                        return titles
                except Exception:
                    continue
            return []

        # Independent provider calls run in parallel to avoid blocking the detail page.
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            tasks = {'cinekivu': pool.submit(cine_similar, sid),
                     'related': pool.submit(internal_related),
                     'detail': pool.submit(internal_details)}
            partial = False
            for name, future in tasks.items():
                try:
                    results[name] = future.result()
                except Exception as exc:
                    log.info('Recommendation source %s unavailable (%s)', name, type(exc).__name__)
                    results[name], partial = [] if name != 'detail' else {}, True

        raw_detail = results['detail'] if isinstance(results['detail'], dict) else {}
        people = normalize_people(raw_detail)
        related = _dedupe_titles(results['cinekivu'] + results['related'], sid)
        sections = []
        if related:
            sections.append({'id': 'similar', 'title': 'Films et séries similaires',
                             'reason': 'Recommandations liées à ce titre, selon les fournisseurs',
                             'items': related})
        cast_ids = [p for p in people if p.get('id')][:2]
        if cast_ids:
            filmography = {}
            for person in cast_ids:
                try:
                    remote = _usable(c.get_staff_titles(person['id']))
                    entries = _dedupe_titles(_items(remote, mapper, 45), sid, 16)
                    if entries:
                        filmography[person['id']] = entries
                except Exception as exc:
                    partial = True
                    log.info('Filmography unavailable (%s)', type(exc).__name__)
            if filmography:
                known = {item['subjectId'] for item in related}
                seen = set()
                by_cast = []
                for person in cast_ids:
                    for item in filmography.get(person['id'], []):
                        item_id = item['subjectId']
                        if item_id not in seen and item_id not in known:
                            by_cast.append(item)
                            seen.add(item_id)
                if by_cast:
                    sections.append({'id': 'cast', 'title': 'Avec les mêmes acteurs',
                                     'reason': 'Autres films ou séries associés aux interprètes',
                                     'items': sorted(by_cast, key=language_rank)[:18]})
        data = {'subjectId': sid, 'cast': people, 'sections': sections,
                'partial': partial, 'source': 'cinekivu+internal',
                'note': 'Similaires selon les fournisseurs ; VF identifiée dans le titre, non vérifiée dans les pistes audio.'}
        # Don't retain an empty failure response long; allow refresh when providers recover.
        _cache_put(key, data, 240 if sections or people else 25)
        return {'code': 0, 'data': data}

    @router.get('/people/{person_id}/titles')
    def person_titles(person_id: str, session_id: str | None = Cookie(None)):
        person = valid_id(person_id)
        c = get_session(session_id)['content']
        try:
            raw = _usable(c.get_staff_titles(person))
            items = _dedupe_titles(_items(raw, mapper, 80), '', 35)
            return {'code': 0, 'data': {'personId': person, 'items': items, 'source': 'internal'}}
        except Exception as exc:
            log.warning('Filmography service failed: %s', type(exc).__name__)
            raise HTTPException(502, 'La filmographie est momentanément indisponible') from exc

    app.include_router(router)
