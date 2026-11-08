#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python_bin="${PYTHON_BIN:-.venv/bin/python}"
"$python_bin" -m pytest tests -q -p api_audit_pytest

fixture_pid=''
cleanup() {
  if [[ -n "$fixture_pid" ]]; then
    kill "$fixture_pid" 2>/dev/null || true
    wait "$fixture_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
mkdir -p postman/reports
if ! "$python_bin" -c 'import json, urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8001/__sdk_fixture/status", timeout=1))["fixture"] == "cinekivu-sdk-http-v1"' 2>/dev/null; then
  "$python_bin" -m uvicorn sdk_fixture_app:app --app-dir tests --host 127.0.0.1 --port 8001 >postman/reports/sdk-fixture.log 2>&1 &
  fixture_pid=$!
  "$python_bin" - <<'PY'
import json, time, urllib.request
for attempt in range(40):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8001/__sdk_fixture/status', timeout=0.5) as response:
            if json.load(response).get('fixture') == 'cinekivu-sdk-http-v1':
                break
    except (OSError, ValueError):
        pass
    time.sleep(0.15)
else:
    raise SystemExit('Serveur de test SDK indisponible ; consulter postman/reports/sdk-fixture.log et le port 8001.')
PY
fi

collection=postman/collections/cinekivu-regression.postman_collection.json
if [[ -n "${POSTMAN_BIN:-}" ]]; then
  "$POSTMAN_BIN" collection run "$collection" "$@"
elif command -v postman >/dev/null 2>&1; then
  postman collection run "$collection" "$@"
elif [[ -n "${NEWMAN_BIN:-}" ]]; then
  "$NEWMAN_BIN" run "$collection" "$@"
elif command -v newman >/dev/null 2>&1; then
  newman run "$collection" "$@"
elif [[ -x build/api-tools/node_modules/.bin/newman ]]; then
  build/api-tools/node_modules/.bin/newman run "$collection" "$@"
else
  echo 'Installer Postman CLI ou Newman, puis relancer. Le backend doit écouter sur 127.0.0.1:8000.' >&2
  exit 1
fi
