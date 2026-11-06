"""Validate film filters on the running backend without requesting video media."""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = 'http://127.0.0.1:8000'


def main():
    session = requests.Session()
    response = session.get(BASE + '/api/v1/web/filter/options', params={'category': 'movies'}, timeout=30)
    response.raise_for_status()
    options = response.json()['data']
    assert options['source'] == 'web-bff-native-filter-config' and not options['stale']
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(),
              'reference': 'https://movieboxhd.net/fr/web/film?utm_source=',
              'options': options, 'queries': []}
    combined = {'category': 'movies', 'genre': 'Comedy', 'country': 'France',
                'year': '2024', 'language': 'French dub', 'sort': 'latest'}
    cases = [
        {'category': 'movies'},
        combined,
        {**combined, 'page': 2},
        {**combined, 'sort': 'rating', 'rate': [7, 10]},
        {'category': 'movies', 'year': '2010s', 'country': 'United States', 'sort': 'latest'},
        {'category': 'movies', 'language': 'Hindi dub', 'sort': 'hottest'},
        {**combined, 'sort': 'hottest'},
    ]
    for query in cases:
        response = session.post(BASE + '/api/v1/web/filter', json=query, timeout=45)
        response.raise_for_status()
        data = response.json()['data']
        assert data['source'] == 'web-bff-native-filter' and data['channelId'] == 1
        assert data['perPage'] == 28 and not data['stale'] and not data['partial']
        assert data['page'] == query.get('page', 1)
        items = data['items']
        assert len(items) <= 28 and all(item['subjectType'] == 1 for item in items)
        year = query.get('year')
        if year:
            assert all(item['releaseDate'].startswith(year[:3] if year.endswith('s') else year) for item in items)
        if query.get('rate'):
            assert all(query['rate'][0] <= float(item['score']) <= query['rate'][1] for item in items)
        if query.get('sort') == 'latest':
            dates = [item['releaseDate'] for item in items]
            assert dates == sorted(dates, reverse=True)
        if query.get('sort') == 'rating':
            ratings = [float(item['score']) for item in items]
            assert ratings == sorted(ratings, reverse=True)
        entry = {'query': query, 'http': response.status_code, 'page': data['page'],
                 'count': len(items), 'hasMore': data['hasMore'], 'nextPage': data['nextPage'],
                 'total': data['totalLiveItems'], 'ids': [item['subjectId'] for item in items],
                 'sample': [{key: item.get(key) for key in ('subjectId', 'title', 'releaseDate', 'score')}
                            for item in items[:2]]}
        report['queries'].append(entry)
        print(json.dumps({key: value for key, value in entry.items() if key != 'ids'}, ensure_ascii=False), flush=True)
    # The chosen combined query has multiple pages: verify the API actually moves forward.
    assert report['queries'][1]['hasMore']
    assert report['queries'][2]['ids'] and set(report['queries'][1]['ids']).isdisjoint(report['queries'][2]['ids'])
    path = Path(__file__).resolve().parents[1] / 'docs' / 'native_films_live_validation.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Validation enregistrée : {path}', flush=True)


if __name__ == '__main__':
    main()
