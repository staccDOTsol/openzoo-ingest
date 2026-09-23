#!/bin/bash
# ensure_engine must not execute or delete a tree it cannot verify.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../daemon/run.sh
source "$ROOT/daemon/run.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

SHARE="$tmp/share"
mkdir -p "$SHARE"
MANAGED_LECORE="$SHARE/leCore"
LECORE_COMMIT="ea456c3f1d4b05acc6ac1ea5a9946d77759f8d72"
LECORE_URL="$tmp/no-such-remote"

# A custom path that only has the one filename the old check looked for.
bad="$tmp/custom-engine"
mkdir -p "$bad/holographic/caching_and_storage"
echo planted-custom > "$bad/holographic/caching_and_storage/holographic_index.py"
LECORE="$bad"
if ensure_engine; then
  echo "custom unverified engine was trusted" >&2
  exit 1
fi
grep -q planted-custom "$bad/holographic/caching_and_storage/holographic_index.py"
test ! -d "$MANAGED_LECORE/.git"

# The managed path is ours, but a failed fetch must leave the existing tree alone.
mkdir -p "$MANAGED_LECORE/holographic/caching_and_storage"
echo planted-managed > "$MANAGED_LECORE/holographic/caching_and_storage/holographic_index.py"
LECORE="$MANAGED_LECORE"
if ensure_engine; then
  echo "unverified managed engine was trusted" >&2
  exit 1
fi
grep -q planted-managed "$MANAGED_LECORE/holographic/caching_and_storage/holographic_index.py"
test ! -e "$SHARE"/leCore.stage.*

# A custom tree that really is the pinned commit is trusted and not rewritten.
good="$tmp/good"
mkdir -p "$good"
git -C "$good" init -q -b main
git -C "$good" config user.email engine-verify@example.com
git -C "$good" config user.name engine-verify
while IFS= read -r rel; do
  mkdir -p "$good/$(dirname "$rel")"
  printf '# %s\n' "$rel" > "$good/$rel"
done <<'EOF'
CAPABILITIES.md
holographic_service.py
holographic/agents_and_reasoning/holographic_ai.py
holographic/caching_and_storage/holographic_index.py
holographic/caching_and_storage/holographic_knowledgestore.py
holographic/mesh_and_geometry/holographic_planshape.py
holographic/misc/holographic_determinism.py
holographic/misc/holographic_superposed.py
EOF
git -C "$good" add -A
git -C "$good" commit -q -m engine
LECORE="$good"
LECORE_COMMIT="$(git -C "$good" rev-parse HEAD)"
ensure_engine
grep -q holographic_index.py "$good/holographic/caching_and_storage/holographic_index.py"
echo "engine policy ok"
