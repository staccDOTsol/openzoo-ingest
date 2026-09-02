#!/bin/bash
# openzoo-ingest installer. Re-run to update. Linux with systemd --user.
#
#   curl -fsSL https://raw.githubusercontent.com/staccDOTsol/openzoo-ingest/main/install.sh | bash
#
# Everything lands under ~/.local/share/openzoo-ingest and ~/.config/openzoo-ingest.
# Nothing here opens a port beyond loopback and nothing here sends data off the
# machine: the shared-brain egress stays unset until you write it into
# ~/.config/openzoo-ingest/env yourself.
set -e
REPO=https://github.com/staccDOTsol/openzoo-ingest
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/openzoo-ingest"
SRC="$SHARE/src"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/openzoo-ingest"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

for c in git python3; do
  command -v "$c" >/dev/null || { echo "need $c"; exit 1; }
done

# The checkout is ours and nobody edits it in place, so an update is "become
# origin/main", not a merge: a stray mode bit or a hand edit must never leave
# the box on a stale version with "unstaged changes" as the only clue.
# PLUGIN MODE: `omarchy plugin add` already cloned this repository into
# ~/.config/omarchy/plugins/openzoo-ingest, so the checkout IS that directory.
# Link it into the place the units expect and skip the clone/update; the
# plugin manager owns updates from here on.
if [ "${1:-}" = "--plugin" ]; then
  PLUGIN_DIR="${2:?--plugin needs the plugin directory}"
  mkdir -p "$SHARE"
  if [ -e "$SRC" ] && [ ! -L "$SRC" ]; then
    mv "$SRC" "$SRC.pre-plugin.$(date +%s)"
  fi
  ln -sfn "$PLUGIN_DIR" "$SRC"
  echo "==> plugin checkout linked: $SRC -> $PLUGIN_DIR"
elif [ -d "$SRC/.git" ]; then
  echo "==> updating $SRC"
  git -C "$SRC" fetch --quiet --depth 1 origin main
  git -C "$SRC" reset --quiet --hard origin/main
else
  echo "==> cloning into $SRC"
  mkdir -p "$SHARE"
  git clone --depth 1 --quiet "$REPO" "$SRC"
fi

# Optional extractors. Omarchy already ships tesseract; poppler gives pdftotext.
if command -v pacman >/dev/null && ! command -v pdftotext >/dev/null; then
  echo "==> installing poppler (pdftotext) — sudo may prompt"
  sudo pacman -S --needed --noconfirm poppler >/dev/null 2>&1 || echo "    skipped (pdf files will not bind)"
fi

mkdir -p "$CONF" "$UNITS" "$HOME/.local/bin"
[ -f "$CONF/env" ] || cat >"$CONF/env" <<'EOF'
# openzoo-ingest — read by the systemd unit. Everything is optional.
# Defaults are LOCAL ONLY: nothing below leaves the machine until you uncomment it.
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
ln -sf "$SRC/bin/openzoo-ingest" "$HOME/.local/bin/openzoo-ingest"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now openzoo-ingest.timer openzoo-lecore.service 2>/dev/null || true
  rm -f "$UNITS/openzoo-ingest.service" "$UNITS/openzoo-ingest.timer" "$UNITS/openzoo-lecore.service" "$HOME/.local/bin/openzoo-ingest"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "openzoo-ingest units removed. Memory kept at $SHARE (delete it yourself if you want it gone)."
  exit 0
fi

if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  cp "$SRC/systemd/"*.service "$SRC/systemd/"*.timer "$UNITS/"
  systemctl --user daemon-reload
  systemctl --user enable --now openzoo-lecore.service
  systemctl --user enable --now openzoo-ingest.timer
  echo "==> daemon + 10-minute timer enabled (systemctl --user status openzoo-ingest.timer)"
  # First bind now, so status is real before the timer's first tick. The first
  # daemon start bootstraps a venv, numpy and a shallow leCore clone, which can
  # take a few minutes on a fresh box — wait for it rather than racing it.
  printf "==> waiting for the leCore daemon on 127.0.0.1:8787 "
  up=0
  for _ in $(seq 1 240); do
    if curl -s -m 1 -o /dev/null http://127.0.0.1:8787/; then up=1; break; fi
    if ! systemctl --user is-active --quiet openzoo-lecore.service; then
      echo; echo "!!! openzoo-lecore.service is not running:"
      journalctl --user -u openzoo-lecore -n 20 --no-pager 2>/dev/null | sed 's/^/    /'
      break
    fi
    printf "."; sleep 1
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

echo
echo "openzoo-ingest installed."
echo "  status:   openzoo-ingest status"
echo "  live bar: openzoo-ingest watch"
echo "  recall:   openzoo-ingest recall \"what did I copy about the deploy\""
echo "  bind now: openzoo-ingest run | openzoo-ingest file ~/Documents | openzoo-ingest url https://..."
echo "  log:      ${XDG_STATE_HOME:-$HOME/.local/state}/openzoo-ingest/ingest.log"
