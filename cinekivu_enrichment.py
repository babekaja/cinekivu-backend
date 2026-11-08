"""Bounded, read-only anime/manga metadata search from V28 research tools."""
from typing import Literal

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query

from cinekivu_metadata_cache import MetadataCache

cache = MetadataCache(ttl_seconds=600, max_entries=128)
ANILIST_QUERY = '''query($name:String!,$type:MediaType!,$limit:Int!){
  Page(page:1,perPage:$limit){media(search:$name,type:$type,isAdult:false){
    id idMal title{romaji english native} format episodes chapters volumes
    countryOfOrigin siteUrl
  }}
}'''


def fetch_metadata(provider: str, kind: str, query: str, limit: int) -> dict:
    try:
        with httpx.Client(timeout=8, follow_redirects=False, headers={
                'Accept': 'application/json', 'User-Agent': 'CineKivuMetadata/1.0'}) as client:
            if provider == 'anilist':
                res = client.post('https://graphql.anilist.co', json={
                    'query': ANILIST_QUERY,
                    'variables': {'name': query, 'type': kind.upper(), 'limit': limit}})
            else:
                res = client.get('https://api.mangadex.org/manga', params={
                    'title': query, 'limit': limit, 'contentRating[]': 'safe'})
        if res.status_code == 429:
            raise HTTPException(503, 'Fournisseur de métadonnées temporairement limité')
        res.raise_for_status()
        payload = res.json()
        if not isinstance(payload, dict) or payload.get('errors'):
            raise ValueError('Invalid metadata response')
        items = []
        if provider == 'anilist':
            rows = payload['data']['Page']['media']
            for row in rows[:limit]:
                titles = row['title']
                items.append({'externalId': str(row['id']),
                              'title': titles.get('english') or titles.get('romaji') or titles.get('native'),
                              'titles': titles, 'format': row.get('format'),
                              'episodes': row.get('episodes'), 'chapters': row.get('chapters'),
                              'volumes': row.get('volumes'), 'url': row.get('siteUrl'),
                              'attribution': 'AniList'})
        else:
            for row in payload['data'][:limit]:
                attrs = row['attributes']
                titles = attrs['title']
                items.append({'externalId': str(row['id']),
                              'title': titles.get('fr') or titles.get('en') or next(iter(titles.values()), ''),
                              'titles': titles, 'alternativeTitles': attrs.get('altTitles', []),
                              'translatedLanguages': attrs.get('availableTranslatedLanguages', []),
                              'url': f'https://mangadex.org/title/{row["id"]}',
                              'attribution': 'MangaDex'})
        return {'code': 0, 'data': {'provider': provider, 'kind': kind, 'query': query,
                                    'items': items, 'streamAvailability': 'unknown'}}
    except httpx.TimeoutException:
        raise HTTPException(504, 'Délai du fournisseur de métadonnées dépassé') from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(502, 'Réponse du fournisseur de métadonnées indisponible ou invalide') from None


def install_enrichment_routes(app: FastAPI):
    router = APIRouter(prefix='/api/v1/enrichment', tags=['CineKivu / métadonnées anime et manga'])

    @router.get('/search')
    def search(provider: Literal['anilist', 'mangadex'], kind: Literal['anime', 'manga'],
               q: str = Query(min_length=2, max_length=120), limit: int = Query(5, ge=1, le=5)):
        query = ' '.join(q.split())
        if len(query) < 2:
            raise HTTPException(422, 'Titre trop court')
        if provider == 'mangadex' and kind != 'manga':
            raise HTTPException(422, 'MangaDex fournit des métadonnées manga uniquement')
        return cache.get_or_load((provider, kind, query.casefold(), limit),
                                 lambda: fetch_metadata(provider, kind, query, limit))

    app.include_router(router)
