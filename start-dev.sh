#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v uvicorn >/dev/null; then echo 'uvicorn absent : installez les dépendances Python.' >&2; exit 1; fi
exec uvicorn moviebox_api_server:app --host 127.0.0.1 --port 8000
