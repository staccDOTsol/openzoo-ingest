"""Minimal Crockford Base32 ULID (time+random), no deps."""
import os
import time

_ALPH = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _enc(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPH[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_ulid() -> str:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big") & ((1 << 80) - 1)
    return _enc(ms, 10) + _enc(rand, 16)
