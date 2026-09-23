"""Per-install secret for the local memory daemon.

Loopback is shared by every account on the machine, so a token shipped in the
source tree is not an access control. The public string "hrr-lab-token" used
to be that token. It is now rejected. A strong secret is generated on first
run into a user-owned file (mode 0600) and requests without it are refused.

The token path is always ~/.config/openzoo-ingest/service-token unless
HRR_TOKEN_FILE is set. It deliberately ignores XDG_CONFIG_HOME so a shared
config directory cannot redirect the secret.
"""
from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path

PUBLIC_DEFAULT_TOKEN = "hrr-lab-token"
MIN_TOKEN_LEN = 32
# Ingest batches are capped at 400_000 characters. 8 MiB leaves room for JSON
# overhead without letting one request pin tens of gigabytes of RAM.
DEFAULT_MAX_BODY = 8 * 1024 * 1024
# An env override may lower the cap or raise it, but never back to 32 GiB.
HARD_MAX_BODY = 32 * 1024 * 1024
_MAX_TOKEN_FILE_BYTES = 4096

REJECTED_TOKENS = frozenset({
    PUBLIC_DEFAULT_TOKEN,
    "changeme",
    "password",
    "secret",
    "token",
    "test",
})


class AuthError(Exception):
    """The process must not serve memory until this is resolved."""


def default_token_path() -> Path:
    return Path.home() / ".config" / "openzoo-ingest" / "service-token"


def token_file_path() -> Path:
    explicit = os.environ.get("HRR_TOKEN_FILE")
    if explicit:
        return Path(explicit)
    return default_token_path()


def assert_strong_token(token: str, source: str) -> str:
    token = (token or "").strip()
    if token in REJECTED_TOKENS or token.lower() in REJECTED_TOKENS:
        raise AuthError("refusing public or placeholder token from %s" % source)
    if len(token) < MIN_TOKEN_LEN:
        raise AuthError(
            "token from %s must be at least %d characters" % (source, MIN_TOKEN_LEN)
        )
    if any(ch.isspace() for ch in token):
        raise AuthError("token from %s must not contain whitespace" % source)
    return token


def resolve_max_body(raw: str | None = None) -> int:
    """Sane default, with a hard ceiling so HRR_MAX_BODY cannot restore 32 GiB."""
    if raw is None:
        raw = os.environ.get("HRR_MAX_BODY")
    if raw in (None, ""):
        return DEFAULT_MAX_BODY
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY
    if n <= 0:
        return DEFAULT_MAX_BODY
    if n > HARD_MAX_BODY:
        return HARD_MAX_BODY
    return n


def check_content_length(header_value: str | None, max_body: int) -> tuple[int, str | None]:
    """Return (nbytes, error). error is None, 'bad', or 'too_large'.

    A missing length is an empty body. A negative length must not be passed to
    rfile.read: read(-1) consumes the socket until EOF.
    """
    if header_value is None or header_value == "":
        return 0, None
    try:
        n = int(str(header_value).strip())
    except (TypeError, ValueError):
        return 0, "bad"
    if n < 0 or n > max_body:
        return 0, "too_large"
    return n, None


def presented_token(headers) -> str:
    if headers is None:
        return ""
    direct = headers.get("X-HRR-Service-Token") or ""
    if direct:
        return str(direct).strip()
    auth = headers.get("Authorization") or ""
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def auth_header_ok(expected: str, headers) -> bool:
    """Constant-time check. The public default never matches, even if configured."""
    try:
        expected = assert_strong_token(expected or "", "server")
    except AuthError:
        return False
    presented = presented_token(headers)
    if not presented:
        return False
    try:
        return hmac.compare_digest(presented, expected)
    except (TypeError, ValueError):
        return False


def load_service_token(env_var: str = "HRR_SERVICE_TOKEN", *, generate: bool = True) -> str:
    """Return the strong secret, generating it into the token file when allowed.

    An explicit env value that is the old public token is ignored so a leftover
    OPENZOO_LECORE_TOKEN=hrr-lab-token cannot override the per-install file.
    Any other explicit env value must itself be strong.
    """
    env = os.environ.get(env_var)
    if env not in (None, ""):
        candidate = env.strip()
        if candidate != PUBLIC_DEFAULT_TOKEN:
            return assert_strong_token(candidate, env_var)
    return _token_from_file(token_file_path(), generate=generate)


def _token_from_file(path: Path, *, generate: bool) -> str:
    if not path.exists() and not path.is_symlink():
        if not generate:
            raise AuthError(
                "no service token at %s. Run install.sh or start the daemon once. "
                "The public default token is not accepted." % path
            )
        fresh = secrets.token_urlsafe(32)
        try:
            _write_new_token(path, fresh)
            return fresh
        except FileExistsError:
            pass
    current = _read_token_file(path)
    if _rejected(current):
        if not generate:
            raise AuthError(
                "service token at %s is a public default and will not be used" % path
            )
        fresh = secrets.token_urlsafe(32)
        _replace_token(path, fresh)
        return fresh
    return assert_strong_token(current, str(path))


def _rejected(token: str) -> bool:
    token = (token or "").strip()
    return (not token) or token in REJECTED_TOKENS or token.lower() in REJECTED_TOKENS


def _prepare_parent(path: Path) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.name == "openzoo-ingest":
        os.chmod(parent, 0o700)
    _reject_world_writable_parent(parent)


def _reject_world_writable_parent(parent: Path) -> None:
    mode = parent.stat().st_mode
    if mode & 0o002:
        raise AuthError("refusing token in world-writable directory %s" % parent)


def _read_token_file(path: Path) -> str:
    if path.is_symlink():
        raise AuthError("refusing symlinked token file %s" % path)
    _reject_world_writable_parent(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AuthError("cannot read token file %s: %s" % (path, exc)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthError("token file %s is not a regular file" % path)
        if st.st_uid != os.geteuid():
            raise AuthError("token file %s is not owned by this user" % path)
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)
            st = os.fstat(fd)
            if st.st_mode & 0o077:
                raise AuthError("token file %s is group or world accessible" % path)
        data = bytearray()
        while len(data) <= _MAX_TOKEN_FILE_BYTES:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            data += chunk
        if len(data) > _MAX_TOKEN_FILE_BYTES:
            raise AuthError("token file %s is unexpectedly large" % path)
    finally:
        os.close(fd)
    return data.decode("utf-8").strip()


def _write_new_token(path: Path, token: str) -> None:
    _prepare_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    else:
        os.close(fd)


def _replace_token(path: Path, token: str) -> None:
    """Replace a placeholder token. rename() swaps the directory entry, not a symlink target."""
    _prepare_parent(path)
    tmp = path.parent / (".service-token.%d.tmp" % os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
