#!/bin/bash
# Start the local leCore daemon for openzoo-ingest. Idempotent bootstrap:
# a venv with numpy, a shallow clone of leCore (the engine), then the sidecar.
# Loopback only — HRR_HOST defaults to 127.0.0.1 and this never changes it.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/openzoo-ingest"
VENV="$SHARE/venv"
LECORE="${LECORE_PATH:-$SHARE/leCore}"
DATA="${HRR_DATA_DIR:-$SHARE/lecore-memory}"
PORT="${HRR_PORT:-8787}"

mkdir -p "$SHARE" "$DATA"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating venv at $VENV"
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import numpy' 2>/dev/null || {
  echo "==> installing numpy"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check numpy
}

# PINNED to an exact commit: the engine is code this daemon executes, so it is
# fetched by SHA, never by branch. Bump LECORE_COMMIT deliberately.
LECORE_COMMIT="${LECORE_COMMIT:-ea456c3f1d4b05acc6ac1ea5a9946d77759f8d72}"
if [ ! -f "$LECORE/holographic/caching_and_storage/holographic_index.py" ]; then
  echo "==> fetching leCore (the engine) at $LECORE_COMMIT into $LECORE"
  git init -q "$LECORE"
  git -C "$LECORE" fetch -q --depth 1 https://github.com/staccDOTsol/leCore "$LECORE_COMMIT"
  git -C "$LECORE" checkout -q FETCH_HEAD
fi

exec env LECORE_PATH="$LECORE" HRR_DATA_DIR="$DATA" HRR_PORT="$PORT" \
  HRR_HOST="${HRR_HOST:-127.0.0.1}" SEMANTIC_STAGE="${SEMANTIC_STAGE:-off}" \
  "$VENV/bin/python" "$HERE/server.py"
