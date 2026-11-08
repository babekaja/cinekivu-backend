#!/usr/bin/env sh
set -eu
# Playback tokens live in this process; keep exactly one worker per instance.
exec python -m uvicorn moviebox_api_server:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
