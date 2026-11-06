"""Native ranking navigation from the provider's current public pages."""
import re

import httpx
from fastapi import HTTPException

from cinekivu_source import api as cine
from cinekivu_native_catalogue import decode_nuxt_subtree

RANKING_ID = re.compile(r'^\d{1,21}$')
RANKING_PATH = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
TOP_FILMS_RANKING_ID = '7760430879155407216'
WEB_RANKING_USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36')


def read_ranking_navigation(ranking_id: str = '', path: str = '') -> dict:
    if path and (not RANKING_PATH.fullmatch(path) or not RANKING_ID.fullmatch(ranking_id)):
        raise HTTPException(422, 'Lien de classement invalide')
    try:
        extra = {'follow_redirects': False} if isinstance(cine.engine.session, httpx.Client) else {'allow_redirects': False}
        response = cine.engine.session.get(
            'https://movieboxhd.net/fr/ranking-list' + (f'/{path}' if path else ''),
            params={'id': ranking_id, 'page_from': 'more_SUBJECTS_MOVIE'} if path else None,
            headers={'User-Agent': WEB_RANKING_USER_AGENT, 'Accept-Language': 'fr-FR,fr;q=0.9',
                     'X-Request-Lang': 'fr'}, timeout=11, **extra)
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            raise ValueError('ranking_navigation_unavailable')
        data = decode_nuxt_subtree(response.text,
            {'rankingList', 'currentId', 'subjectList', 'title'},
            {'rankingList', 'currentId', 'title'})
        tabs = []
        for item in data.get('rankingList') or []:
            if not isinstance(item, dict):
                continue
            sid, slug = str(item.get('id') or ''), str(item.get('path') or '')
            if RANKING_ID.fullmatch(sid) and RANKING_PATH.fullmatch(slug):
                tabs.append({'id': sid, 'title': str(item.get('name') or '')[:120], 'path': slug})
        current = str(data.get('currentId') or '')
        if not tabs or not any(tab['id'] == current for tab in tabs):
            raise ValueError('invalid_ranking_navigation')
        return {'tabs': tabs, 'currentId': current, 'title': str(data.get('title') or 'Classements'),
                'source': 'web-bff-native-rankings', 'stale': False}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'Les classements du fournisseur sont indisponibles') from exc


def resolve_ranking_navigation(ranking_id: str, path: str, home_loader) -> dict:
    if ranking_id and not RANKING_ID.fullmatch(ranking_id):
        raise HTTPException(422, 'Identifiant de classement invalide')
    if path:
        return read_ranking_navigation(ranking_id, path)
    if ranking_id:
        home = home_loader()
        for block in (home.get('data') or {}).get('operatingList') or []:
            if not isinstance(block, dict):
                continue
            if ranking_id not in (str(block.get('genreTopId') or ''), str(block.get('opId') or '')):
                continue
            sid, slug = str(block.get('genreTopId') or ''), str(block.get('detailPath') or '')
            if RANKING_ID.fullmatch(sid) and RANKING_PATH.fullmatch(slug):
                return read_ranking_navigation(sid, slug)
    data = read_ranking_navigation()
    if ranking_id:
        selected = next((tab for tab in data['tabs'] if tab['id'] == ranking_id), None)
        if not selected:
            raise HTTPException(404, 'Classement absent de la navigation actuelle')
        data.update(currentId=ranking_id, title=selected['title'])
    return data
