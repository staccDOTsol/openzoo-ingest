"""leCore's OWN service surface, exposed through the sidecar — docs/ZOO.md §1, §4, §9, §11.

WHY A BRIDGE AND NOT A REIMPLEMENTATION. Everything the sidecar previously used
from leCore was four hand-picked modules (holographic_ai, Index, planshape,
superposed) wired into our own ranking and recall. That left 1,944 faculties
unreachable from openzoo and, worse, meant our version of a faculty could drift
from his: we had a *coverage note* where leCore has a calibrated abstention
gate, and no void machinery at all. This module delegates to the same
`Service.dispatch("POST", "/invoke", ...)` that `holographic_mcp.py` calls, so
there is one implementation and it is his.

WHAT RIDES ON EVERY CALL (ZOO.md §4 + §11.1): `cost` = {elapsed_ms,
payload_bytes} measured per call, and `receipt` = {input_sha256, output_sha256,
deterministic} over the canonical (name, args) pair. Because the engine is
deterministic, that pair is a re-verifiable claim about what was computed — the
proxy can bill reality and settle disputes by re-running. Wall-clock stays in
cost and NEVER in the receipt: time is the one thing an honest re-run will not
reproduce.

The gate is inherited, not rebuilt: private faculties are refused by the
Service itself, exactly as they are over MCP.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

# Flat import to match the rest of the sidecar (`import ouroboros`, `import rank`),
# which is loaded with service/ on sys.path rather than as a package.
try:
    import _lecore
except ImportError:  # package-style import, when someone loads it that way
    from . import _lecore

_SERVICE = None


def service():
    """The live leCore Service, built once. Raises if leCore is not installed —
    a bridge that silently degrades to a hand-rolled fallback would reintroduce
    the exact drift this module exists to remove."""
    global _SERVICE
    if _SERVICE is None:
        _lecore.lecore_root()  # raises with the actionable message if absent
        import sys
        root = _lecore.lecore_root()
        if root not in sys.path:
            sys.path.insert(0, root)
        from holographic_service import Service  # noqa: E402
        _SERVICE = Service()
    return _SERVICE


def _canonical(name: str, args: Dict[str, Any]) -> bytes:
    return json.dumps({"name": name, "args": args or {}},
                      sort_keys=True, separators=(",", ":")).encode()


def _meta(name: str, args: Dict[str, Any], out: Any, t0: float) -> Dict[str, Any]:
    """ZOO.md §4 cost census + §11.1 receipt, on the same wire as leCore's MCP."""
    payload = json.dumps(out, default=str).encode()
    return {
        "cost": {"elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 4),
                 "payload_bytes": len(payload)},
        "receipt": {"input_sha256": hashlib.sha256(_canonical(name, args)).hexdigest(),
                    "output_sha256": hashlib.sha256(payload).hexdigest(),
                    "deterministic": True},
    }


def find(query: str, top: int = 8) -> Dict[str, Any]:
    """Rule-0 search over the live catalog — 'before implementing any algorithm'."""
    t0 = time.perf_counter()
    hits = service().mind.find_capability(query)[: int(top)]
    out: List[Dict[str, Any]] = [
        {"name": h.name, "does": (h.does or "")[:200], "method": getattr(h, "method", None)}
        for h in hits
    ]
    return {"object": "lecore.find", "query": query, "hits": out, **_meta("find", {"query": query}, out, t0)}


def describe(name: str) -> Dict[str, Any]:
    t0 = time.perf_counter()
    hits = service().mind.find_capability(name)[:1]
    if not hits:
        return {"object": "lecore.describe", "error": "no capability matching %r" % name}
    h = hits[0]
    out = {"name": h.name, "does": h.does, "example": h.example,
           "method": getattr(h, "method", None), "aliases": list(h.aliases or ())}
    return {"object": "lecore.describe", **out, **_meta("describe", {"name": name}, out, t0)}


def invoke(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Any faculty, through leCore's own dispatch. Private-method refusals and
    the {"__bytes_b64__": ...} wire convention are inherited from the Service."""
    t0 = time.perf_counter()
    a = args or {}
    out = service().dispatch("POST", "/invoke", {"name": name, "args": a})
    return {"object": "lecore.invoke", "name": name, "result": out, **_meta(name, a, out, t0)}


def catalog_size() -> int:
    try:
        return int(len(service().mind._capability_catalog().all()))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# CALIBRATED ABSTENTION FOR RECALL — leCore docs/ZOO.md §2, via mind.permutation_null.
#
# The failure this exists to kill: retrieval hands back its top_k best-scoring
# chunks whether or not any of them are actually about the query. Ranking always
# produces a ranking. MEASURED in production: "list every mention of pump.fun"
# over a 7,000-chunk corpus returned a confident, complete-sounding answer while
# an agent's grep found evidence retrieval never surfaced.
#
# The gate is leCore's own shuffle null (the SETI/particle-physics discipline):
# score the real query, re-run the IDENTICAL scorer on resamples that destroy
# the structure, and report whether the real score stands out. If it does not,
# the honest answer is "I cannot vouch for this retrieval" — not a ranked list.
#
# PROCEDURE-MATCH IS THE CALLER'S JOB, and his docstring says so as a kept
# negative: a wrong resample_fn gives a mis-calibrated null. So the null here is
# NOT random noise — it is a query of the SAME token count drawn from THIS
# CORPUS'S OWN vocabulary. Out-of-vocabulary gibberish would score ~0 against
# BM25 and gate nothing, which is a null that always passes and therefore lies.
#
# COST: n_null re-rankings of the same corpus. At n_null=99 that is ~100x a
# recall, so this is OPT-IN per request, never the default path. 99 is chosen
# deliberately: it is the smallest null that can resolve p=0.01 (the +1 plug
# means p is never exactly 0), matching the alpha we actually gate at.

def recall_gate(query, top_score, vocab, score_fn, n_null=99, alpha=0.01, seed=0):
    """Is `top_score` for `query` distinguishable from this corpus's own noise?

    score_fn(query_string) -> float   the SAME scorer that produced top_score
    vocab: list[str]                  the corpus's own tokens (the null's source)

    Returns leCore's verdict dict plus `abstain` — True when the real query
    scored no better than scrambled queries, i.e. the ranking is ranking noise.
    """
    import random as _r

    toks = [t for t in str(query).split() if t]
    n = max(1, len(toks))
    if not vocab:
        return {"abstain": False, "reason": "no vocabulary — gate not run"}

    def resample_fn(rng):
        # A same-length query built from the corpus's own words: destroys the
        # QUERY's structure while preserving everything else about the task.
        try:
            picks = [vocab[int(rng.integers(0, len(vocab)))] for _ in range(n)]
        except AttributeError:                      # a stdlib Random, not numpy
            picks = [vocab[_r.Random(0).randrange(len(vocab))] for _ in range(n)]
        return " ".join(picks)

    out = service().mind.permutation_null(
        float(top_score), lambda q: float(score_fn(q)), resample_fn,
        n_null=int(n_null), seed=int(seed), alpha=float(alpha), side="greater")
    p = float(out.get("p", 1.0))
    out["abstain"] = bool(p > float(alpha))
    out["alpha"] = float(alpha)
    return out
