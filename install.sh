#!/bin/bash
# openzoo-ingest installer. Re-run to update. Linux with systemd --user.
#
#   omarchy plugin add https://github.com/staccDOTsol/openzoo-ingest.git --enable
#   (or, from a checkout: bash install.sh)
#
# Paths this plugin may replace are the ones it already owns: a checkout whose
# manifest is openzoo-ingest, the launcher symlink into that checkout, and
# user units that carry the openzoo-ingest-owned marker (or the legacy
# ExecStart / timer description this plugin shipped). Anything else at those
# paths is left alone and the install stops.
#
# The service token is ~/.config/openzoo-ingest/service-token (mode 0600).
# Nothing here opens a port beyond loopback, and nothing here sends data off
# the machine: shared-brain egress stays unset until you write it into the
# env file yourself.
set -euo pipefail

REPO=https://github.com/staccDOTsol/openzoo-ingest
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/openzoo-ingest"
SRC="$SHARE/src"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/openzoo-ingest"
# Token path matches the systemd unit (%h/.config/...) and service_auth.py.
# It does not follow XDG_CONFIG_HOME.
TOKEN_DIR="$HOME/.config/openzoo-ingest"
TOKEN_FILE="$TOKEN_DIR/service-token"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LAUNCHER="$HOME/.local/bin/openzoo-ingest"
UNIT_NAMES=(openzoo-lecore.service openzoo-ingest.service openzoo-ingest.timer)

origin_is_ours() {
  case "$1" in
    https://github.com/staccDOTsol/openzoo-ingest|\
    https://github.com/staccDOTsol/openzoo-ingest.git|\
    git@github.com:staccDOTsol/openzoo-ingest|\
    git@github.com:staccDOTsol/openzoo-ingest.git)
      return 0
      ;;
  esac
  return 1
}

assert_plugin_checkout() {
  local d="$1"
  [ -f "$d/manifest.json" ] || return 1
  python3 - "$d/manifest.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        manifest = json.load(fh)
except Exception:
    sys.exit(1)
repo = str(manifest.get("repository") or "").rstrip("/")
ok = (
    manifest.get("id") == "openzoo-ingest"
    and repo.endswith("github.com/staccDOTsol/openzoo-ingest")
)
sys.exit(0 if ok else 1)
PY
}

unit_owned() {
  local p="$1"
  [ -f "$p" ] && [ ! -L "$p" ] || return 1
  if grep -q 'openzoo-ingest-owned' "$p"; then
    return 0
  fi
  # Units shipped before the marker. The ExecStart path is this plugin's.
  if grep -q 'openzoo-ingest/src/' "$p"; then
    return 0
  fi
  # Legacy timer had no ExecStart, only this description.
  if grep -qx 'Description=Run openzoo ingest every 10 minutes' "$p" \
     && ! grep -q '^Exec' "$p"; then
    return 0
  fi
  return 1
}

launcher_owned() {
  local p="$1"
  if [ -L "$p" ]; then
    local raw resolved srcbin
    raw=$(readlink "$p" || true)
    case "$raw" in
      *"/openzoo-ingest/src/bin/openzoo-ingest"|*"/openzoo-ingest/bin/openzoo-ingest")
        return 0
        ;;
    esac
    resolved=$(readlink -f "$p" 2>/dev/null || true)
    srcbin=$(readlink -f "$SRC/bin/openzoo-ingest" 2>/dev/null || true)
    if [ -n "$resolved" ] && [ -n "$srcbin" ] && [ "$resolved" = "$srcbin" ]; then
      return 0
    fi
    return 1
  fi
  if [ -f "$p" ] && grep -q 'openzoo-ingest-owned' "$p"; then
    return 0
  fi
  return 1
}

preflight_owned_paths() {
  local u dest
  for u in "${UNIT_NAMES[@]}"; do
    dest="$UNITS/$u"
    if [ -L "$dest" ]; then
      echo "refusing to overwrite symlink $dest" >&2
      return 1
    fi
    if [ -e "$dest" ] && ! unit_owned "$dest"; then
      echo "refusing to overwrite $dest (not owned by openzoo-ingest)" >&2
      return 1
    fi
  done
  if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    if ! launcher_owned "$LAUNCHER"; then
      echo "refusing to overwrite $LAUNCHER (not owned by openzoo-ingest)" >&2
      return 1
    fi
  fi
}

link_plugin_checkout() {
  local plugin_dir target
  plugin_dir=$(readlink -f -- "$1") || {
    echo "refusing: cannot resolve plugin directory $1" >&2
    return 1
  }
  if ! assert_plugin_checkout "$plugin_dir"; then
    echo "refusing: $plugin_dir is not the openzoo-ingest plugin" >&2
    return 1
  fi
  mkdir -p "$SHARE"
  if [ -L "$SRC" ]; then
    target=$(readlink -f "$SRC" 2>/dev/null || true)
    if [ -n "$target" ] && [ "$target" != "$plugin_dir" ] && ! assert_plugin_checkout "$target"; then
      echo "refusing to retarget $SRC (points at $target, not this plugin)" >&2
      return 1
    fi
  elif [ -d "$SRC" ]; then
    if [ "$(readlink -f "$SRC")" = "$plugin_dir" ]; then
      echo "==> plugin checkout is $SRC"
      return 0
    fi
    if ! assert_plugin_checkout "$SRC"; then
      echo "refusing to move $SRC (not an openzoo-ingest checkout)" >&2
      return 1
    fi
    mv -- "$SRC" "$SRC.pre-plugin.$(date +%s)"
  elif [ -e "$SRC" ]; then
    echo "refusing to replace $SRC" >&2
    return 1
  fi
  ln -sfn "$plugin_dir" "$SRC"
  echo "==> plugin checkout linked: $SRC -> $plugin_dir"
}

update_or_clone_src() {
  local origin
  if [ -e "$SRC/.git" ] || [ -d "$SRC/.git" ]; then
    if ! assert_plugin_checkout "$SRC"; then
      echo "refusing to update $SRC (manifest is not openzoo-ingest)" >&2
      return 1
    fi
    origin=$(git -C "$SRC" remote get-url origin 2>/dev/null || true)
    if ! origin_is_ours "$origin"; then
      echo "refusing to reset $SRC (origin '${origin}' is not this plugin)" >&2
      return 1
    fi
    echo "==> updating $SRC"
    git -C "$SRC" fetch --quiet --depth 1 origin main || return 1
    git -C "$SRC" reset --quiet --hard origin/main || return 1
  elif [ -e "$SRC" ] || [ -L "$SRC" ]; then
    echo "refusing to clone over existing $SRC" >&2
    return 1
  else
    echo "==> cloning into $SRC"
    mkdir -p "$SHARE"
    git clone --depth 1 --quiet "$REPO" "$SRC"
  fi
}

install_env_file() {
  local old_umask
  mkdir -p "$CONF"
  if [ ! -e "$CONF/env" ]; then
    old_umask=$(umask)
    umask 077
    cat >"$CONF/env" <<'EOF'
# openzoo-ingest — read by the systemd unit. Everything is optional.
# Defaults are LOCAL ONLY: nothing below leaves the machine until you uncomment it.
#
# The daemon bearer is not in this file. It is the per-install secret
# ~/.config/openzoo-ingest/service-token (mode 0600). The old public token
# is rejected.
#
# OPENZOO_NOTIFY=1                 desktop notification per run (0 to silence)
#
# EGRESS 1 — screenshot vision. Sends screenshot PIXELS through the local proxy
# to a hosted model, N paid calls per run. 0 (default) = tesseract OCR only, local.
# OPENZOO_VISION_CAP=10
#
# EGRESS 2 — shared brain. Every bound item is ALSO sent here.
# OPENZOO_BRAIN_URL=https://api.openzoo.fun
# OPENZOO_BRAIN_KEY=
EOF
    umask "$old_umask"
  elif [ -L "$CONF/env" ]; then
    echo "leaving $CONF/env unchanged (symlink)" >&2
  elif [ -f "$CONF/env" ] && grep -q 'openzoo-ingest' "$CONF/env"; then
    chmod 600 "$CONF/env" || true
  else
    echo "leaving $CONF/env unchanged (not an openzoo-ingest config)" >&2
  fi
}

ensure_service_token() {
  mkdir -p "$TOKEN_DIR"
  chmod 700 "$TOKEN_DIR" || true
  HRR_TOKEN_FILE="$TOKEN_FILE" \
    PYTHONPATH="$INSTALL_ROOT/daemon${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c 'from service_auth import load_service_token; load_service_token()'
  chmod 600 "$TOKEN_FILE"
}

install_launcher() {
  mkdir -p "$(dirname "$LAUNCHER")"
  if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    if ! launcher_owned "$LAUNCHER"; then
      echo "refusing to overwrite $LAUNCHER (not owned by openzoo-ingest)" >&2
      return 1
    fi
  fi
  ln -sfn "$SRC/bin/openzoo-ingest" "$LAUNCHER"
}

install_units() {
  local f dest
  local -a srcs=()
  mkdir -p "$UNITS"
  for f in "$SRC/systemd/"*.service "$SRC/systemd/"*.timer; do
    [ -f "$f" ] || continue
    dest="$UNITS/$(basename "$f")"
    if [ -L "$dest" ]; then
      echo "refusing to overwrite symlink $dest" >&2
      return 1
    fi
    if [ -e "$dest" ] && ! unit_owned "$dest"; then
      echo "refusing to overwrite $dest (not owned by openzoo-ingest)" >&2
      return 1
    fi
    srcs+=("$f")
  done
  if [ "${#srcs[@]}" -eq 0 ]; then
    echo "refusing to install units: none found in $SRC/systemd" >&2
    return 1
  fi
  for f in "${srcs[@]}"; do
    cp -- "$f" "$UNITS/$(basename "$f")"
  done
}

uninstall_owned() {
  local u dest
  for u in "${UNIT_NAMES[@]}"; do
    dest="$UNITS/$u"
    if [ ! -e "$dest" ] && [ ! -L "$dest" ]; then
      continue
    fi
    if [ -L "$dest" ] || ! unit_owned "$dest"; then
      echo "leaving $dest in place (not owned by openzoo-ingest)"
      continue
    fi
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now "$u" 2>/dev/null || true
    fi
    rm -f -- "$dest"
  done
  if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    if launcher_owned "$LAUNCHER"; then
      rm -f -- "$LAUNCHER"
    else
      echo "leaving $LAUNCHER in place (not owned by openzoo-ingest)"
    fi
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  echo "openzoo-ingest units removed. Memory kept at $SHARE (delete it yourself if you want it gone)."
  echo "Service token kept at $TOKEN_FILE."
}

enable_systemd() {
  # Unit files are ours to install whenever the destination is owned or absent.
  # Enabling them requires a working systemd --user; without it the files still
  # land so a later login can start them, and an unrelated file is never copied over.
  install_units || return 1
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now openzoo-lecore.service
    systemctl --user enable --now openzoo-ingest.timer
    echo "==> daemon + 10-minute timer enabled (systemctl --user status openzoo-ingest.timer)"
    printf "==> waiting for the leCore daemon on 127.0.0.1:%s " "${HRR_PORT:-8787}"
    local up=0
    local _
    for _ in $(seq 1 240); do
      if curl -s -m 1 -o /dev/null "http://127.0.0.1:${HRR_PORT:-8787}/"; then up=1; break; fi
      if ! systemctl --user is-active --quiet openzoo-lecore.service; then
        echo
        echo "!!! openzoo-lecore.service is not running:"
        journalctl --user -u openzoo-lecore -n 20 --no-pager 2>/dev/null | sed 's/^/    /' || true
        break
      fi
      printf "."
      sleep 1
    done
    echo
    if [ "$up" = 1 ]; then
      systemctl --user start openzoo-ingest.service || true
    else
      echo "    daemon not up yet; the timer retries every 10 minutes, or run: openzoo-ingest run"
    fi
  else
    echo "==> no systemd --user: start the daemon with $SRC/daemon/run.sh and run openzoo-ingest on a cron"
  fi
}

main() {
  local cmd="${1:-}"
  if [ "$cmd" = "--uninstall" ]; then
    uninstall_owned
    return 0
  fi

  local c
  for c in git python3; do
    command -v "$c" >/dev/null || { echo "need $c" >&2; return 1; }
  done

  preflight_owned_paths || return 1

  if [ "$cmd" = "--plugin" ]; then
    if [ -z "${2:-}" ]; then
      echo "--plugin needs the plugin directory" >&2
      return 1
    fi
    link_plugin_checkout "$2" || return 1
  else
    update_or_clone_src || return 1
  fi

  command -v pdftotext >/dev/null || echo "==> pdftotext not found: PDFs will not bind (optional: install poppler)"

  install_env_file || return 1
  ensure_service_token || return 1
  install_launcher || return 1
  enable_systemd || return 1

  echo
  echo "openzoo-ingest installed."
  echo "  status:   openzoo-ingest status"
  echo "  live bar: openzoo-ingest watch"
  echo "  recall:   openzoo-ingest recall \"what did I copy about the deploy\""
  echo "  bind now: openzoo-ingest run | openzoo-ingest file ~/Documents | openzoo-ingest url https://..."
  echo "  log:      ${XDG_STATE_HOME:-$HOME/.local/state}/openzoo-ingest/ingest.log"
  echo "  token:    $TOKEN_FILE (mode 0600; not printed)"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@" || exit $?
fi
