#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python -m app.corpus.build
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
