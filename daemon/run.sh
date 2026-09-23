#!/bin/bash
# Start the local leCore daemon for openzoo-ingest.
# Bootstraps a private venv with pinned numpy, then refuses to import leCore
# unless the worktree matches the pinned commit (every blob, not one filename).
# Loopback is only the bind address. The access control is the per-install
# token at ~/.config/openzoo-ingest/service-token.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LECORE_URL="${LECORE_URL:-https://github.com/staccDOTsol/leCore}"
# PINNED to an exact commit: the engine is code this daemon executes.
# Bump LECORE_COMMIT deliberately. A branch name is rejected by verify_engine.py.
LECORE_COMMIT="${LECORE_COMMIT:-ea456c3f1d4b05acc6ac1ea5a9946d77759f8d72}"

_canon() {
  readlink -f -- "$1" 2>/dev/null || printf '%s\n' "$1"
}

verify_engine_tree() {
  python3 "$HERE/verify_engine.py" --root "$1" --commit "$2"
}

_is_managed_engine() {
  [ "$(_canon "$1")" = "$(_canon "$2")" ]
}

# Fetch into a directory this process just created. Global and system git
# config are ignored so a url.insteadOf rewrite cannot redirect the pin.
fetch_pinned_into() {
  local dest="$1"
  local commit="$2"
  mkdir -p "$dest"
  git -C "$dest" init -q || return 1
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
    git -C "$dest" fetch -q --depth 1 "$LECORE_URL" "$commit" || return 1
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
    git -C "$dest" checkout -q --force FETCH_HEAD || return 1
}

# $LECORE, $MANAGED_LECORE, $SHARE, $LECORE_COMMIT, $LECORE_URL
ensure_engine() {
  if verify_engine_tree "$LECORE" "$LECORE_COMMIT"; then
    return 0
  fi
  if ! _is_managed_engine "$MANAGED_LECORE" "$LECORE"; then
    echo "refusing to trust engine at $LECORE (not pinned commit $LECORE_COMMIT)" >&2
    return 1
  fi
  if [ -L "$LECORE" ]; then
    echo "refusing to replace symlinked engine path $LECORE" >&2
    return 1
  fi
  local stage rejected
  mkdir -p "$SHARE"
  stage=$(mktemp -d "$SHARE/leCore.stage.XXXXXX")
  if ! fetch_pinned_into "$stage" "$LECORE_COMMIT"; then
    rm -rf -- "$stage"
    echo "failed to fetch pinned leCore $LECORE_COMMIT from $LECORE_URL" >&2
    return 1
  fi
  if ! verify_engine_tree "$stage" "$LECORE_COMMIT"; then
    rm -rf -- "$stage"
    echo "fetched leCore failed verification; not installing it" >&2
    return 1
  fi
  if [ -e "$LECORE" ]; then
    rejected="$SHARE/leCore.rejected.$$"
    mv -- "$LECORE" "$rejected"
    if ! mv -- "$stage" "$LECORE"; then
      mv -- "$rejected" "$LECORE" || true
      rm -rf -- "$stage"
      echo "failed to install verified engine" >&2
      return 1
    fi
    case "$rejected" in
      "$SHARE"/leCore.rejected.*) rm -rf -- "$rejected" ;;
      *) echo "refusing to delete unexpected path $rejected" >&2 ;;
    esac
  else
    mv -- "$stage" "$LECORE"
  fi
  verify_engine_tree "$LECORE" "$LECORE_COMMIT"
}

read_numpy_pin() {
  local line
  line=$(grep -E '^numpy==[0-9]+\.[0-9]+\.[0-9]+([abrc][0-9]+)?$' "$HERE/requirements.txt" | head -n 1 || true)
  if [ -z "${line}" ]; then
    echo "daemon/requirements.txt must pin numpy to an exact version" >&2
    return 1
  fi
  printf '%s\n' "${line#numpy==}"
}

install_pinned_numpy() {
  local pin
  pin=$(read_numpy_pin) || return 1
  if "$VENV/bin/python" -c 'import numpy,sys; raise SystemExit(0 if numpy.__version__==sys.argv[1] else 1)' "$pin" 2>/dev/null; then
    return 0
  fi
  echo "==> installing numpy==$pin"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --only-binary=numpy "numpy==$pin"
  "$VENV/bin/python" -c 'import numpy,sys; raise SystemExit(0 if numpy.__version__==sys.argv[1] else 1)' "$pin"
}

main() {
  umask 077
  local share venv data port conf token_file
  share="${XDG_DATA_HOME:-$HOME/.local/share}/openzoo-ingest"
  venv="$share/venv"
  data="${HRR_DATA_DIR:-$share/lecore-memory}"
  port="${HRR_PORT:-8787}"
  conf="$HOME/.config/openzoo-ingest"
  token_file="${HRR_TOKEN_FILE:-$conf/service-token}"
  SHARE="$share"
  MANAGED_LECORE="$share/leCore"
  LECORE="${LECORE_PATH:-$MANAGED_LECORE}"
  VENV="$venv"

  mkdir -p "$share" "$data" "$conf"
  chmod 700 "$data" "$conf" || true

  if [ ! -x "$venv/bin/python" ]; then
    echo "==> creating venv at $venv"
    python3 -m venv "$venv"
  fi
  install_pinned_numpy

  if ! ensure_engine; then
    echo "leCore engine at $LECORE is not pinned commit $LECORE_COMMIT; refusing to start" >&2
    exit 1
  fi

  # Do not write bytecode into the verified engine tree.
  exec env \
    PYTHONDONTWRITEBYTECODE=1 \
    LECORE_PATH="$LECORE" \
    HRR_DATA_DIR="$data" \
    HRR_PORT="$port" \
    HRR_HOST="${HRR_HOST:-127.0.0.1}" \
    HRR_TOKEN_FILE="$token_file" \
    SEMANTIC_STAGE="${SEMANTIC_STAGE:-off}" \
    "$venv/bin/python" "$HERE/server.py"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
