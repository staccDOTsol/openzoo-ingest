#!/bin/bash
# Installer must not clobber paths it does not own, and must still update its own.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
stub="$tmp/bin"
mkdir -p "$stub"
log="$tmp/systemctl.log"
cat >"$stub/systemctl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${SYSTEMCTL_LOG:?}"
case "$*" in
  *show-environment*) exit 1 ;;
esac
exit 0
EOF
chmod +x "$stub/systemctl"

new_home() {
  local home="$1"
  mkdir -p "$home"
  export HOME="$home"
  export XDG_CONFIG_HOME="$home/.config"
  export XDG_DATA_HOME="$home/.local/share"
  export PATH="$stub:$PATH"
  export SYSTEMCTL_LOG="$log"
  # shellcheck source=../install.sh
  source "$ROOT/install.sh"
}

mode_of() {
  stat -c '%a' "$1"
}

# --- refuse to move an unrelated checkout ---
new_home "$tmp/home-foreign-src"
mkdir -p "$SRC/not-ours"
echo 'do-not-move' > "$SRC/not-ours/keep"
if link_plugin_checkout "$ROOT"; then
  echo "moved an unrelated directory" >&2
  exit 1
fi
test -d "$SRC/not-ours"
grep -q do-not-move "$SRC/not-ours/keep"
test ! -L "$SRC"

# --- refuse to clone over an existing non-repo path ---
new_home "$tmp/home-file-src"
mkdir -p "$(dirname "$SRC")"
echo 'not-a-checkout' > "$SRC"
if update_or_clone_src; then
  echo "cloned over an existing path" >&2
  exit 1
fi
grep -q not-a-checkout "$SRC"

# --- refuse to reset a git checkout that is not this plugin's origin ---
new_home "$tmp/home-origin"
mkdir -p "$SRC"
git -C "$SRC" init -q -b main
git -C "$SRC" config user.email install-test@example.com
git -C "$SRC" config user.name install-test
cp "$ROOT/manifest.json" "$SRC/manifest.json"
echo canary > "$SRC/canary"
git -C "$SRC" add manifest.json canary
git -C "$SRC" commit -q -m canary
git -C "$SRC" remote add origin https://example.com/not-ours.git
if update_or_clone_src; then
  echo "reset a foreign origin" >&2
  exit 1
fi
grep -q canary "$SRC/canary"

# --- plugin mode moves a checkout we own and links the plugin dir ---
new_home "$tmp/home-own-src"
mkdir -p "$SRC"
cp "$ROOT/manifest.json" "$SRC/manifest.json"
echo ours > "$SRC/ours"
link_plugin_checkout "$ROOT"
test -L "$SRC"
test "$(readlink -f "$SRC")" = "$(readlink -f "$ROOT")"
test -f "$SHARE"/src.pre-plugin.*/ours

# --- foreign unit and launcher stop install before any of our writes ---
new_home "$tmp/home-foreign-unit"
mkdir -p "$UNITS" "$(dirname "$LAUNCHER")"
printf 'FOREIGN-UNIT\n' > "$UNITS/openzoo-lecore.service"
printf 'FOREIGN-LAUNCHER\n' > "$LAUNCHER"
if main --plugin "$ROOT"; then
  echo "install clobbered a foreign unit" >&2
  exit 1
fi
grep -q FOREIGN-UNIT "$UNITS/openzoo-lecore.service"
grep -q FOREIGN-LAUNCHER "$LAUNCHER"
test ! -e "$SRC"
test ! -e "$TOKEN_FILE"

# --- legacy timer is ours; a fresh install writes the marker and the token ---
new_home "$tmp/home-legacy"
mkdir -p "$UNITS"
cat >"$UNITS/openzoo-ingest.timer" <<'EOF'
[Unit]
Description=Run openzoo ingest every 10 minutes

[Timer]
OnBootSec=2min

[Install]
WantedBy=timers.target
EOF
main --plugin "$ROOT"
grep -q 'openzoo-ingest-owned' "$UNITS/openzoo-ingest.timer"
grep -q 'openzoo-ingest-owned' "$UNITS/openzoo-lecore.service"
grep -q 'openzoo-ingest-owned' "$UNITS/openzoo-ingest.service"
test -L "$LAUNCHER"
test -L "$SRC"
test -f "$TOKEN_FILE"
test "$(mode_of "$TOKEN_FILE")" = "600"
test "$(mode_of "$TOKEN_DIR")" = "700"
token_a="$(cat "$TOKEN_FILE")"
test "${#token_a}" -ge 32
test "$token_a" != "hrr-lab-token"
# stdout of a second ensure must not contain the secret, and must not rotate it
ensure_service_token >"$tmp/token-out"
test "$(cat "$TOKEN_FILE")" = "$token_a"
if grep -q "$token_a" "$tmp/token-out"; then
  echo "service token was printed" >&2
  exit 1
fi
# env template does not grant the old public token
grep -q 'service-token' "$CONF/env"
if grep -q 'hrr-lab-token' "$CONF/env"; then
  echo "env file still documents the public token as usable" >&2
  exit 1
fi
test "$(mode_of "$CONF/env")" = "600"

# --- foreign env file is not rewritten ---
new_home "$tmp/home-env"
mkdir -p "$CONF"
printf 'FOREIGN_ENV=1\n' > "$CONF/env"
install_env_file
grep -q FOREIGN_ENV "$CONF/env"
if grep -q 'service-token' "$CONF/env"; then
  echo "foreign env file was rewritten" >&2
  exit 1
fi

# --- uninstall removes owned units and leaves foreign ones and the token ---
new_home "$tmp/home-uninstall"
main --plugin "$ROOT"
token_b="$(cat "$TOKEN_FILE")"
printf 'FOREIGN-TIMER\n' > "$UNITS/openzoo-other.timer"
# A same-named unit we do not own must survive. Replace one owned unit with foreign
# content after install by writing beside the owned set: use a path uninstall
# would consider if it matched the name. Here the timer is ours; a different
# filename is not in the removal list. Also plant a foreign launcher by
# replacing the symlink with a regular file before uninstall.
rm -f "$LAUNCHER"
printf 'FOREIGN-LAUNCHER\n' > "$LAUNCHER"
uninstall_owned
test ! -e "$UNITS/openzoo-lecore.service"
test ! -e "$UNITS/openzoo-ingest.service"
test ! -e "$UNITS/openzoo-ingest.timer"
grep -q FOREIGN-TIMER "$UNITS/openzoo-other.timer"
grep -q FOREIGN-LAUNCHER "$LAUNCHER"
test "$(cat "$TOKEN_FILE")" = "$token_b"
test -d "$SHARE"

# --- uninstall of a foreign unit that uses our unit name leaves it ---
new_home "$tmp/home-uninstall-foreign"
mkdir -p "$UNITS"
printf 'FOREIGN-SERVICE\n' > "$UNITS/openzoo-lecore.service"
uninstall_owned
grep -q FOREIGN-SERVICE "$UNITS/openzoo-lecore.service"

# --- a public token file is replaced by a strong one ---
new_home "$tmp/home-weak-token"
mkdir -p "$TOKEN_DIR"
printf '%s' 'hrr-lab-token' > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
ensure_service_token
test "$(cat "$TOKEN_FILE")" != "hrr-lab-token"
test "$(mode_of "$TOKEN_FILE")" = "600"

echo "install ownership ok"
