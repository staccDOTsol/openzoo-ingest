#!/bin/bash
# numpy is installed only from the hash-locked wheel list.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../daemon/run.sh
source "$ROOT/daemon/run.sh"

lock="$ROOT/daemon/requirements.lock"
validate_numpy_lock "2.5.3" "$lock"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python3 - "$tmp" <<'PY'
import sys
from pathlib import Path
tmp = Path(sys.argv[1])
digest = "ab" * 32
def write(name, text):
    (tmp / name).write_text(text)
write("ok.lock", f"numpy==2.5.3 \\\n    --hash=sha256:{digest}\n")
write("sdist.lock", "numpy==2.5.3 \\\nnumpy-2.5.3.tar.gz\n")
write("extra.lock", "numpy==2.5.3 \\\nrequests==2.32.0\n")
write("wrong.lock", f"numpy==9.9.9 \\\n    --hash=sha256:{digest}\n")
write("short.lock", "numpy==2.5.3 \\\n    --hash=sha256:abcd\n")
PY

validate_numpy_lock "2.5.3" "$tmp/ok.lock"
if validate_numpy_lock "2.5.3" "$tmp/sdist.lock" 2>/dev/null; then
  echo "sdist line was accepted" >&2
  exit 1
fi
if validate_numpy_lock "2.5.3" "$tmp/extra.lock" 2>/dev/null; then
  echo "second package was accepted" >&2
  exit 1
fi
if validate_numpy_lock "2.5.3" "$tmp/wrong.lock" 2>/dev/null; then
  echo "wrong version was accepted" >&2
  exit 1
fi
if validate_numpy_lock "2.5.3" "$tmp/short.lock" 2>/dev/null; then
  echo "short hash was accepted" >&2
  exit 1
fi
# A hash-shaped sdist digest is not rejected by the lock grammar; the
# committed lock must not contain it, and pip --only-binary rejects sdists.
if grep -q df2d5874ff183595a4ba404edd04f6bd9b5505c1d7708573f6a6c17489a67563 "$lock"; then
  echo "committed lock lists the sdist digest" >&2
  exit 1
fi
if grep -q 'numpy==2\.5\.3\.tar\.gz\|tar\.gz' "$lock"; then
  echo "committed lock names an sdist" >&2
  exit 1
fi

# The installer must pass the lock to pip --require-hashes and must not also
# pass an unhashed numpy== spec. A second start from the same lock must not
# hit the index again.
record="$tmp/record"
ready="$tmp/ready"
export RECORD="$record" READY="$ready"
mkdir -p "$tmp/venv/bin"
cat > "$tmp/venv/bin/python" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$RECORD"
if [[ "$1" == "-c" ]]; then
  case "$2" in
    *hashlib*)
      python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$3"
      exit 0
      ;;
    *numpy*)
      if [[ -f "$READY" ]]; then
        exit 0
      fi
      exit 1
      ;;
  esac
  exit 1
fi
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  touch "$READY"
  exit 0
fi
exit 1
EOF
chmod +x "$tmp/venv/bin/python"
VENV="$tmp/venv"
: > "$record"
install_pinned_numpy
install_pinned_numpy
pip_lines="$(grep -c -- '-m pip ' "$record" || true)"
if [[ "$pip_lines" -ne 1 ]]; then
  echo "expected one hashed install, saw $pip_lines" >&2
  cat "$record" >&2
  exit 1
fi
pip_line="$(grep -- '-m pip ' "$record")"
for flag in --require-hashes --only-binary=numpy --only-binary=:all: --no-deps --force-reinstall; do
  case "$pip_line" in
    *"$flag"*) ;;
    *) echo "missing $flag in: $pip_line" >&2; exit 1 ;;
  esac
done
case "$pip_line" in
  *"-r $lock"*) ;;
  *) echo "pip did not install from the lock: $pip_line" >&2; exit 1 ;;
esac
case "$pip_line" in
  *numpy==*) echo "unhashed numpy spec was passed to pip: $pip_line" >&2; exit 1 ;;
esac
echo "numpy lock ok"
