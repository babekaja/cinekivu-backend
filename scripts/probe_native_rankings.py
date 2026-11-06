"""Verify the three requested native ranking contexts on the running backend."""
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = 'http://127.0.0.1:8000'


def main():
    session = requests.Session()
    def get(path, **params):
        response = session.get(BASE + path, params=params, timeout=40)
        response.raise_for_status()
        return response.json()['data']
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'contexts': [], 'homeLinks': []}
    home = get('/api/v1/web/home')
    for section in home['sections']:
        if section.get('rankingId') in ('5563687109313136144', '7760430879155407216'):
            report['homeLinks'].append({key: section.get(key) for key in ('id', 'title', 'rankingId', 'rankingPath')})
    assert len(report['homeLinks']) == 2
    for requested in ('', '5563687109313136144', '7760430879155407216'):
        navigation = get('/api/v1/web/rankings', ranking_id=requested)
        sid = navigation['currentId']
        assert not navigation['stale'] and sid in {tab['id'] for tab in navigation['tabs']}
        if requested:
            assert sid == requested
        entry = {'requestedId': requested or None, 'navigation': navigation, 'pages': []}
        for page in (1, 2):
            data = get(f'/api/v1/web/rankings/{sid}', page=page)
            assert data['rankingId'] == sid and data['page'] == page and data['perPage'] == 20
            assert data['source'] == 'web-bff-native-ranking' and not data['partial'] and not data['stale']
            assert data['items'] and data['items'][0]['rank'] == (page - 1) * 20 + 1
            assert [item['rank'] for item in data['items']] == sorted(item['rank'] for item in data['items'])
            entry['pages'].append({'page': page, 'count': len(data['items']), 'hasMore': data['hasMore'],
                'nextPage': data['nextPage'], 'total': data['totalLiveItems'],
                'ids': [item['subjectId'] for item in data['items']],
                'sample': [{key: item.get(key) for key in ('subjectId', 'title', 'subjectType', 'rank')}
                           for item in data['items'][:2]]})
        if len(navigation['tabs']) > 1:
            other = navigation['tabs'][1]
            other_data = get(f"/api/v1/web/rankings/{other['id']}")
            assert other_data['rankingId'] == other['id'] and other_data['page'] == 1
            entry['tabSwitch'] = {'id': other['id'], 'label': other['title'], 'count': len(other_data['items'])}
        report['contexts'].append(entry)
        print(json.dumps({'id': sid, 'title': navigation['title'], 'tabs': len(navigation['tabs']),
            'pages': [{key: value for key, value in page.items() if key not in ('ids', 'sample')}
                      for page in entry['pages']]}, ensure_ascii=False), flush=True)
    for section in report['homeLinks']:
        data = get('/api/v1/web/home/rails/' + section['id'])
        assert data['rankingId'] == section['rankingId']
    assert get('/api/v1/web/categories/top10')['rankingId'] == '7760430879155407216'
    path = Path(__file__).resolve().parents[1] / 'docs' / 'native_rankings_live_validation.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Validation enregistrée : {path}', flush=True)


if __name__ == '__main__':
    main()
