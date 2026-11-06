"""Check the running API without downloading media or printing signed URLs.

Run from backend-v3: .venv/bin/python scripts/probe_native_catalogue.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = 'http://127.0.0.1:8000'


def main():
    session = requests.Session()
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'base': BASE,
              'options': {}, 'queries': []}
    response = session.get(BASE + '/health', timeout=10)
    response.raise_for_status()
    assert response.json()['status'] == 'ok'
    for category in ('movies', 'series', 'anime'):
        response = session.get(BASE + '/api/v1/web/filter/options',
                               params={'category': category}, timeout=30)
        response.raise_for_status()
        data = response.json()['data']
        assert data['source'] == 'web-bff-native-filter-config' and not data['stale']
        report['options'][category] = {key: data[key] for key in
            ('genres', 'countries', 'years', 'languages', 'sorts', 'ratingRange')}
    cases = [
        {'category': 'series', 'year': '2024', 'sort': 'latest'},
        {'category': 'series', 'year': '2010s', 'genre': 'Comedy', 'country': 'France',
         'language': 'French dub', 'sort': 'rating'},
        {'category': 'series', 'sort': 'rating', 'rate': [8, 10], 'page': 2},
        {'category': 'series', 'language': 'Hindi dub', 'sort': 'hottest'},
        {'category': 'movies', 'year': '2026', 'sort': 'latest'},
        {'category': 'anime', 'year': '2024', 'sort': 'rating'},
        {'category': 'series', 'keyword': 'Lupin'},
    ]
    for query in cases:
        response = session.post(BASE + '/api/v1/web/filter', json=query, timeout=60)
        response.raise_for_status()
        data = response.json()['data']
        assert not data['stale'] and data['source'].startswith('web-bff-native-')
        items = data['items']
        if query['category'] in ('movies', 'series'):
            assert all(item['subjectType'] == (1 if query['category'] == 'movies' else 2) for item in items)
        if query.get('year', '').endswith('s'):
            assert all(query['year'][:3] == item['releaseDate'][:3] for item in items)
        elif query.get('year'):
            assert all(item['releaseDate'].startswith(query['year']) for item in items)
        if query.get('rate'):
            assert all(query['rate'][0] <= float(item['score']) <= query['rate'][1] for item in items)
        entry = {'query': query, 'http': response.status_code, 'source': data['source'],
                 'page': data['page'], 'count': len(items), 'hasMore': data['hasMore'],
                 'total': data.get('totalLiveItems'), 'partial': data.get('partial'),
                 'sample': [{key: item.get(key) for key in
                     ('subjectId', 'subjectType', 'title', 'releaseDate', 'score')} for item in items[:2]]}
        report['queries'].append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    path = Path(__file__).resolve().parents[1] / 'docs' / 'native_catalogue_live_validation.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Validation enregistrée : {path}', flush=True)


if __name__ == '__main__':
    main()
