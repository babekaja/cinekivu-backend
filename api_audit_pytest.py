"""Optional pytest plugin recording which API contracts the tests execute."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading

_temporary = None
_calls = {}
_lock = threading.Lock()
_operations = []


def pytest_configure(config):
    global _temporary, _operations
    _temporary = tempfile.TemporaryDirectory(prefix='cinekivu-all-api-tests-')
    os.environ['MOVIEBOX_DB_PATH'] = str(Path(_temporary.name) / 'library.sqlite3')
    os.environ['CINEKIVU_FEEDBACK_DB'] = str(Path(_temporary.name) / 'feedback.sqlite3')
    from moviebox_api_server import app
    spec = app.openapi()
    _operations = [(method.upper(), path) for path, methods in spec['paths'].items()
                   for method in methods if method in ('get','post','put','delete','patch','head')]
    patterns = [(path, re.compile('^'+re.sub(r'\{[^}]+\}', '[^/]+', path)+'$'))
                for path in spec['paths']]
    patterns.sort(key=lambda p: (p[0].count('{'), -len(p[0])))

    class CoverageMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await self.app(scope, receive, send)
            status, raw = 500, bytearray()
            async def observe(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                elif message['type'] == 'http.response.body' and len(raw) < 32768:
                    raw.extend(message.get('body', b'')[:32768-len(raw)])
                await send(message)
            try:
                await self.app(scope, receive, observe)
            finally:
                route = getattr(scope.get('route'), 'path', '')
                if route not in spec['paths']:
                    route = next((p for p, regex in patterns if regex.fullmatch(scope['path'])), '')
                if route:
                    semantic_ok = True
                    try:
                        body = json.loads(raw)
                        if isinstance(body, dict):
                            semantic_ok = body.get('code') in (None, 0, 200, '0', '200') and body.get('status') != 'error'
                    except (ValueError, UnicodeError):
                        pass
                    key = scope['method']+' '+route
                    with _lock:
                        row = _calls.setdefault(key, {'statuses': {}, 'successfulResponses': 0, 'calls': 0})
                        row['calls'] += 1
                        row['statuses'][str(status)] = row['statuses'].get(str(status), 0)+1
                        row['successfulResponses'] += int(200 <= status < 400 and semantic_ok)
    app.add_middleware(CoverageMiddleware)


def pytest_sessionfinish(session, exitstatus):
    rows = []
    for method, path in _operations:
        calls = _calls.get(method+' '+path)
        rows.append({'method': method, 'path': path, 'tested': calls is not None,
                     **(calls or {'calls': 0, 'statuses': {}, 'successfulResponses': 0})})
    output = Path('postman/reports/api-route-coverage.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'operations': rows, 'exitCode': int(exitstatus),
        'total': len(rows), 'tested': sum(r['tested'] for r in rows),
        'successTested': sum(r['successfulResponses'] > 0 for r in rows)}, indent=2)+'\n')
    if _temporary:
        _temporary.cleanup()
