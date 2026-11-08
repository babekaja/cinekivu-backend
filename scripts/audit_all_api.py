"""Read-only live API inventory audit. Never sends OTPs or publishes content."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

MOVIE = '8906247916759695608'
SERIES = '6207982430134357800'
RANKING = '7760430879155407216'


def cases():
    simple = ['/', '/health', '/api/health', '/user-info', '/home', '/anime', '/rankings',
              '/discovery', '/trending', '/movies', '/short-tv', '/kids', '/education', '/music',
              '/asian', '/western', '/nollywood', '/tab/2', '/game', '/rooms/recommend',
              '/history', '/watchlist', '/groups/trending', '/groups/interactive', '/debug_history',
              '/sub-proxy', '/api/v1/cinekivu/health', '/api/v1/config', '/api/v1/home',
              '/api/v1/catalog/movies', '/api/v1/catalog/series', '/api/v1/genres',
              '/api/v1/genres/action', '/api/v1/library', '/api/v1/continue-watching',
              '/api/v1/history', '/api/v1/preferences', '/api/v1/recommender/status',
              '/api/v1/recommender/genres', '/api/v1/recommender/movies',
              '/api/v1/recommender/movies/2874', '/api/v1/recommender/movies/2874/recommendations',
              '/api/v1/downloads/capabilities', '/api/v1/web/home', '/api/v1/web/trending',
              '/api/v1/web/popular-searches', '/api/v1/web/rankings',
              '/api/v1/web/rankings/'+RANKING, '/api/v1/web/tabs/2', '/api/v1/web/navigation',
              '/api/v1/web/filter/options?category=movies', '/api/v1/web/categories/new',
              '/api/v1/web/categories/anime', '/api/v1/web/categories/top10',
              '/api/v1/web/snapshot/search?q=Avatar',
              '/api/v1/enrichment/search?provider=anilist&kind=anime&q=Naruto&limit=1',
              '/api/v1/enrichment/search?provider=mangadex&kind=manga&q=Naruto&limit=1']
    simple.append('/api/v1/reference/locales/fr-CD')
    for name in ['search', 'taxonomy', 'stats', 'categories', 'genres', 'formats', 'collections',
                 'languages', 'sources', 'works/WRK-00001', 'works/WRK-00001/editions',
                 'works/WRK-00001/recommendations', 'editions/EDT-00001', 'editions/EDT-00001/tracks']:
        simple.append('/api/v1/reference/'+name)
    for prefix, suffix in [('/detail/', ''), ('/episodes/', ''), ('/subtitles/', ''), ('/post/count/', ''),
                           ('/post/list/', ''), ('/api/v1/titles/', ''), ('/api/v1/titles/', '/seasons'),
                           ('/api/v1/titles/', '/editions'), ('/api/v1/titles/', '/similar'),
                           ('/api/v1/titles/', '/recommendations'),
                           ('/api/v1/cinekivu/titles/', ''), ('/api/v1/cinekivu/titles/', '/editions'),
                           ('/api/v1/cinekivu/titles/', '/episodes'),
                           ('/api/v1/cinekivu/titles/', '/subtitles'),
                           ('/api/v1/web/titles/', ''), ('/api/v1/web/recommendations/', ''),
                           ('/api/v1/web/feedback/', '')]:
        sid = SERIES if 'episodes' in prefix or suffix in ('/seasons', '/episodes') else MOVIE
        simple.append(prefix+sid+suffix)
    for section in ['overview','rating','trailer','cast','reviews']:
        simple.append(f'/api/v1/media/{MOVIE}/{section}')
    simple.extend(['/search?q=Avatar', '/search-suggestions?q=Avatar',
                   '/subtitles/search?query=Avatar', '/api/v1/search?q=Avatar&subject_type=1',
                   '/api/v1/search/autocomplete?q=Avatar', '/api/v1/suggestions?q=Avatar'])
    result = [{'method': 'GET', 'path': path} for path in simple]
    result += [{'method':'GET', 'path':'/api/v1/genres/'+genre} for genre in (
        'comedy','drama','thriller','horror','romance','animation','documentary','science-fiction','fantasy')]
    result += [
        {'method': 'POST', 'path': '/api/v1/web/filter', 'body': {'category': 'movies', 'year': '2026'}},
        {'method': 'POST', 'path': '/api/v1/recommender/recommendations', 'body': {'text': 'space mars astronaut'}},
        {'method': 'POST', 'path': '/api/v1/downloads/resolve', 'body': {'query': 'Avatar', 'year': 2009}},
        {'method': 'GET', 'path': f'/api/v1/cinekivu/titles/{MOVIE}/streams?subject_type=1'},
        {'method': 'GET', 'path': f'/stream/{MOVIE}?quality=720p'},
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--output', default='postman/reports/all-api-live.json')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--only', help='Only paths containing this text')
    parser.add_argument('--media', action='store_true', help='Also check bounded media reads and related live links')
    args = parser.parse_args()
    checks = [c for c in cases() if not args.only or args.only in c['path']]
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'base': args.base,
              'mode': 'read-only live, isolated anonymous session', 'checks': []}
    context = {}
    with httpx.Client(timeout=100, follow_redirects=False) as client:
        client.get(args.base+'/user-info')  # Establish one isolated anonymous cookie before parallel checks.
        spec = client.get(args.base+'/openapi.json').json()
        report['declaredOperations'] = sum(1 for methods in spec['paths'].values()
            for method in methods if method in ('get','post','put','delete','patch','head'))
        def run(check):
            start = time.monotonic()
            result = {'method': check['method'], 'path': check['path']}
            try:
                with client.stream(check['method'], args.base+check['path'], json=check.get('body')) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes(8192):
                        raw.extend(chunk)
                        if len(raw) > 2*1024*1024:
                            raise ValueError('Response too large for metadata audit')
                data = json.loads(raw)
                result.update(status=response.status_code, code=data.get('code') if isinstance(data,dict) else None)
                success = response.status_code < 400 and result['code'] in (None, 0, 200, '0', '200')
                result['ok'] = success
                if response.status_code == 410 and check['path'] in ('/debug_history','/sub-proxy'):
                    result.update(ok=True, intentionallyDisabled=True)
                if response.status_code == 503 and check['path'] in (
                        '/groups/interactive', '/groups/trending', '/rooms/recommend'):
                    result['upstreamUnavailable'] = True
                if not success:
                    detail = data.get('detail') or data.get('message') or data.get('msg') or ''
                    result['error'] = re.sub(r'https?://\S+', '[url]', str(detail))[:240]
                if isinstance(data,dict):
                    payload = data.get('data', data)
                    if isinstance(payload,list):
                        result['items'] = len(payload)
                        if check['path'] == '/rooms/recommend': context['rooms'] = payload
                    elif isinstance(payload,dict):
                        if check['path'] == f'/api/v1/cinekivu/titles/{MOVIE}/subtitles':
                            context['captions'] = payload.get('items', [])
                        if '/cinekivu/titles/' in check['path'] and '/streams?' in check['path']:
                            context['streams'] = payload.get('sources', [])
                        if check['path'] == '/api/v1/downloads/resolve':
                            context['download'] = payload.get('file')
                        if check['path'] == f'/api/v1/titles/{MOVIE}/recommendations':
                            context['people'] = payload.get('cast', [])
                        if check['path'] == '/api/v1/web/home':
                            context['rails'] = payload.get('sections', [])
                        for name in ('items','list','sections','terms','cast','seasons','sources'):
                            if isinstance(payload.get(name),list): result[name+'Count'] = len(payload[name])
                        for name in ('partial','source','stale','playable_count'):
                            if name in payload: result[name] = payload[name]
                    required_nonempty = {
                        '/api/v1/genres/action': 'items', '/search-suggestions?q=Avatar': None,
                        f'/api/v1/cinekivu/titles/{MOVIE}/subtitles': 'items',
                        f'/post/list/{MOVIE}': 'items'}
                    if check['path'] in required_nonempty and result['ok']:
                        key = required_nonempty[check['path']]
                        values = payload.get(key, []) if key and isinstance(payload, dict) else payload
                        if not values:
                            result.update(ok=False, error='Expected nonempty live results')
            except (httpx.HTTPError, ValueError) as exc:
                result.update(ok=False,error=type(exc).__name__)
            result['seconds'] = round(time.monotonic()-start,2)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            return result
        with ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
            for future in as_completed([pool.submit(run, check) for check in checks]):
                report['checks'].append(future.result())
        if args.media:
            room = next((p.get('id') for p in context.get('rooms', []) if p.get('id')), None)
            if room:
                report['checks'].append(run({'method':'GET', 'path':f'/rooms/{room}'}))
            rail = next((p.get('id') for p in context.get('rails', []) if p.get('id')), None)
            if rail:
                report['checks'].append(run({'method':'GET', 'path':f'/api/v1/web/home/rails/{rail}'}))
            actor = next((p.get('id') for p in context.get('people', []) if p.get('id')), None)
            if actor:
                report['checks'].append(run({'method':'GET', 'path':f'/api/v1/people/{actor}/titles'}))
            def media(path, label, kind='mp4', method='GET', language=None):
                row = {'method':method, 'path':label}
                if language: row['language'] = language
                try:
                    assert path.startswith('/api/') or path.startswith('/download/')
                    limit = 32 if kind == 'mp4' else 2*1024*1024
                    raw = bytearray()
                    with client.stream(method, args.base+path, headers={'Range':'bytes=0-31'} if kind == 'mp4' and method == 'GET' else {}) as response:
                        if method != 'HEAD':
                            for chunk in response.iter_raw(min(limit,8192)):
                                raw.extend(chunk[:limit-len(raw)])
                                if len(raw) >= limit: break
                    row.update(status=response.status_code, bytesRead=len(raw))
                    row['ok'] = response.status_code == (206 if kind == 'mp4' and method == 'GET' else 200)
                    if kind == 'caption' and response.status_code == 404:
                        detail = json.loads(raw).get('detail', '')
                        if detail == 'Fichier de sous-titres absent chez le fournisseur':
                            row.update(upstreamUnavailable=True, error=detail)
                    if method != 'HEAD' and row['ok']:
                        if kind == 'mp4': row['ok'] = len(raw)==32 and raw[4:8] in (b'ftyp',b'styp',b'moof',b'sidx')
                        elif kind == 'caption': row['ok'] = raw.startswith(b'WEBVTT')
                        elif kind == 'dash': row['ok'] = ElementTree.fromstring(raw).tag.endswith('MPD')
                        elif kind == 'hls': row['ok'] = raw.startswith(b'#EXTM3U')
                    print(json.dumps(row),flush=True)
                    report['checks'].append(row)
                    return bytes(raw)
                except (httpx.HTTPError, ValueError, AssertionError, ElementTree.ParseError) as exc:
                    row.update(ok=False,error=type(exc).__name__)
                    report['checks'].append(row)
                    return b''
            mp4 = next((s for s in context.get('streams', []) if str(s.get('format')).lower()=='mp4'), None)
            if mp4:
                media(mp4['url'], '/api/play/{token} [Range 32 bytes]')
                media(mp4['url'], '/api/play/{token} [HEAD]', method='HEAD')
                download = mp4.get('download') or {}
                for field in ('url', 'resume_url'):
                    if download.get(field): media(download[field], '/api/download/'+field+' [Range 32 bytes]')
                media('/download/'+MOVIE+'?quality=BEST', '/download/{subject_id} [Range 32 bytes]')
            download = context.get('download') or {}
            if download.get('url'): media(download['url'], '/api/v1/downloads/assets/{token} [Range 32 bytes]')
            captions = context.get('captions') or []
            for caption in captions:
                media(caption['url'], '/api/v1/cinekivu/captions/{token}.vtt', 'caption',
                      language=caption.get('lang'))
            for kind in ('DASH','HLS'):
                source = next((s for s in context.get('streams', []) if s.get('format')==kind), None)
                if source:
                    raw = media(source['url'], '/api/adaptive/{token}/'+kind, 'dash' if kind=='DASH' else 'hls')
                    if kind=='HLS' and raw:
                        lines=raw.decode().splitlines()
                        child=next((x for x in lines if x and not x.startswith('#')), None)
                        if child:
                            child_path=urljoin(source['url'], child)
                            playlist=media(child_path, '/api/adaptive/{token}/variant.m3u8', 'hls')
                            match=re.search(r'#EXT-X-MAP:URI="([^"]+)"',playlist.decode())
                            if match: media(urljoin(child_path,match[1]), '/api/adaptive/{token}/asset/init [Range 32 bytes]')
    report['summary'] = {'total':len(report['checks']),'passed':sum(c['ok'] for c in report['checks']),
                         'upstreamUnavailable':sum(c.get('upstreamUnavailable',False) for c in report['checks']),
                         'failed':sum(not c['ok'] and not c.get('upstreamUnavailable') for c in report['checks'])}
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report['summary']),flush=True)
    return 1 if report['summary']['failed'] else 2 if report['summary']['upstreamUnavailable'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
