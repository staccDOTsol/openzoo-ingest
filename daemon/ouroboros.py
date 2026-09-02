"""OUROBOROS — the model's external memory partition, managed server-side.

Moose's name (leCore `docs/ZOO.md` §7-8, `docs/NOTES_concepts.md`): leCore consuming
the memory produced by the model that has leCore installed inside it, and feeding it
back. This module is the DURABLE end of that loop — §8's external-memory partition:
"assign a partition (a directory; one per tenant) as the model's external memory, and
regard it as an ordinary leCore data structure."

It is deliberately the LIGHT lane. The partition is a `KnowledgeStore` (ids, content
hashes, dedupe, tags, file-rooted persistence — outlives the process, pinned), and
ranking reuses the sidecar's own `rank_bm25` (leCore's Okapi BM25). Neither needs
`UnifiedMind`, which the sidecar refuses to construct because it OOMs the box
(`_lecore.py`). The heavy faculties (`void_explore`, `lecore_find/invoke`) that DO need
the full mind belong on the GPU lane, not here.

Every call returns `_meta`:
  - `lecore.cost`  {elapsed_ms, payload_bytes} — measured per call; the census in ZOO.md
    §4 showed compute and wire diverge ~400:1, so an x402 proxy bills these two numbers.
  - `lecore.receipt` {input_sha256, output_sha256, deterministic:true} — proof-of-inference
    (§11): outputs are a function of (op, args) alone, so the sha pair is a re-verifiable
    claim. "Charge once, serve the hash."
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# leCore's own BM25 ranker, already imported by the sidecar — mind-free.
from rank import rank_bm25  # noqa: E402

# Root for all tenant partitions. Under the data volume so it survives restarts.
_MEMORY_ROOT = os.environ.get("HRR_MEMORY_ROOT") or os.path.join(
    os.environ.get("HRR_DATA_DIR", "/workspace/hrr-context/data"), "ouroboros"
)

# Tenant ids come off the wire; keep the on-disk path from escaping the root.
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _tenant_dir(tenant: Optional[str]) -> str:
    t = _SAFE.sub("_", str(tenant or "shared"))[:120] or "shared"
    return os.path.join(_MEMORY_ROOT, t)


_stores: Dict[str, Any] = {}
_lock = threading.Lock()


def _store(tenant: Optional[str]):
    """Per-tenant KnowledgeStore, cached. The store persists to its directory, so
    a fresh process over the same root finds the same memories (§8: pinned)."""
    key = _tenant_dir(tenant)
    with _lock:
        ks = _stores.get(key)
        if ks is None:
            from _lecore import lecore_root  # noqa: E402
            root = lecore_root()
            if root not in sys.path:
                sys.path.insert(0, root)
            from holographic.caching_and_storage.holographic_knowledgestore import (  # noqa: E402
                KnowledgeStore,
            )
            os.makedirs(key, exist_ok=True)
            ks = KnowledgeStore(key)
            _stores[key] = ks
        return ks


def _meta(op: str, args: Dict[str, Any], out: Any, t0: float) -> Dict[str, Any]:
    text = json.dumps(out, default=str)
    canon = json.dumps({"op": op, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "lecore.cost": {"elapsed_ms": round((time.perf_counter() - t0) * 1e3, 3),
                        "payload_bytes": len(text)},
        "lecore.receipt": {"input_sha256": hashlib.sha256(canon.encode()).hexdigest(),
                           "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "deterministic": True},
    }


def _entry_id(added: Any) -> str:
    """KnowledgeStore.add returns a list of minted ids (['note-0000']); older paths
    may return a dict or a bare string. Normalise to one id string."""
    if isinstance(added, (list, tuple)) and added:
        return str(added[0])
    if isinstance(added, dict):
        return str(added.get("id"))
    return str(added)


def memory_write(tenant: Optional[str], text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Write a fact into the tenant's partition — the ouroboros MOUTH writing the
    model's durable memory. Dedup + hashing are the store's; we mint one entry."""
    t0 = time.perf_counter()
    if not isinstance(text, str) or not text.strip():
        return {"error": "text is required"}
    ks = _store(tenant)
    with _lock:
        added = ks.add(text, kind="note", source="model", tags=tuple(tags or ()))
        ks.save()
    out = {"id": _entry_id(added), "stored": True, "entries": len(ks.entries)}
    out["_meta"] = _meta("memory_write", {"text": text, "tags": tags or []}, out, t0)
    return out


def memory_search(tenant: Optional[str], query: str, top: int = 4,
                  tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ranked recall over the tenant's partition, best first — the ouroboros
    reading the model's own durable memory. Ranks `entries` with leCore's BM25
    directly (KnowledgeStore.search would need a mind; the entries list does not)."""
    t0 = time.perf_counter()
    if not isinstance(query, str) or not query.strip():
        return {"error": "query is required"}
    ks = _store(tenant)
    want = set(tags) if tags else None
    with _lock:
        pool = [e for e in ks.entries
                if want is None or (want & set(e.get("tags") or []))]
    # rank_bm25 takes {"text": ...} dicts and returns [(item, score)], the item
    # being the exact dict we passed — so we can carry ids through it.
    items = [{"text": e["text"], "_id": e.get("id"), "_tags": list(e.get("tags") or [])} for e in pool]
    ranked = rank_bm25(query, items, top_k=max(1, int(top)))
    hits = [{"id": it.get("_id"), "text": it.get("text"), "tags": it.get("_tags", []),
             "score": round(float(s), 4)} for it, s in ranked]
    out = {"hits": hits, "searched": len(pool)}
    out["_meta"] = _meta("memory_search", {"query": query, "top": top, "tags": tags or []}, out, t0)
    return out
