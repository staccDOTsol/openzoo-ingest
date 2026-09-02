#!/usr/bin/env python3
"""HRR Context dogfood service — frozen internal attach/power API (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import numpy as np

# local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rank import (  # noqa: E402
    build_bm25,
    build_vec_index,
    estimate_tokens,
    rank_bm25,
    rank_bm25_prebuilt,
    rank_items,
    rank_stored,
    normalize_query as rank_normalize_query,
    tokenize,
)
from store import ContextStore, StoreError  # noqa: E402
import semantic_stage  # noqa: E402  — imports nothing heavy; cold when SEMANTIC_STAGE=off

VERSION = "0.1.0-dogfood"
DEFAULT_BUDGET = 1024
DEFAULT_TOKEN = "hrr-lab-token"
DEFAULT_PORT = 7090
MAX_BODY = int(os.environ.get("HRR_MAX_BODY") or 34_359_738_368)  # 32GiB
# BM25 hydration caps. Above EITHER, recall stays on the bind-time vec path —
# decompressing every text is exactly what 502'd recall at ~40M words on 19MB
# items. Zoo chunks are ~600 chars so hydrating them is cheap; a fat legacy
# bind is not.
HRR_BM25_MAX_ITEMS = int(os.environ.get("HRR_BM25_MAX_ITEMS") or 60_000)
HRR_BM25_MAX_CHARS = int(os.environ.get("HRR_BM25_MAX_CHARS") or 80_000_000)
# Corpus cache budget. Fly box is 4GB; hydrated corpora can be 100s of MB, so
# cap total cached bytes hard and evict WHOLE contexts LRU-first. 0 disables.
HRR_CACHE_MAX_BYTES = int(os.environ.get("HRR_CACHE_MAX_BYTES") or 1_073_741_824)


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class _CtxEntry:
    __slots__ = (
        "version", "items", "texts", "bm25", "bm25_items", "nbytes",
        "vec_index", "vec_built", "vec_lock",
    )


class ContextCache:
    """(tenant, context_id) → hydrated corpus, validated by store version.

    MEASURED 2026-08-14: deployed recall p50 was ~394ms on 600 chunks / ~2.1s
    at 2400 while the rank call itself was 0.1–0.4ms — every request reloaded
    state.npz four times, tolist()'d every 1024-dim vec, and rebuilt BM25 from
    scratch, all under the store RLock. This cache pays that once per mutation.

    Version token = ContextStore.state_version (state.npz mtime_ns+size).
    _write rewrites state.npz on every bind/unbind, so ANY mutation
    invalidates on the next lookup. LRU-bounded by estimated bytes
    (HRR_CACHE_MAX_BYTES, default 1GiB); eviction drops whole contexts.
    Builds run on ONE thread per key (no cold-burst stampede) and OUTSIDE the
    store RLock — hydrate + BM25 fit, then swap the entry in.
    """

    def __init__(self, store: ContextStore, max_bytes: int = HRR_CACHE_MAX_BYTES):
        self.store = store
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple, _CtxEntry]" = OrderedDict()
        self._building: Dict[tuple, threading.Lock] = {}
        self._total = 0

    def lookup(self, tenant_id: str, context_id: str) -> Optional[_CtxEntry]:
        """Fresh entry, or None when the context is uncacheable (legacy JSON
        store, cache disabled, or a writer raced the build — caller falls back
        to the uncached per-request path). Raises StoreError like the store."""
        if self.max_bytes <= 0 or not context_id:
            return None
        ver = self.store.state_version(context_id)
        if ver is None:
            return None
        key = (tenant_id, context_id)
        with self._lock:
            e = self._entries.get(key)
            if e is not None and e.version == ver:
                self._entries.move_to_end(key)
                return e
            bl = self._building.setdefault(key, threading.Lock())
        with bl:
            ver = self.store.state_version(context_id)
            if ver is None:
                return None
            with self._lock:
                e = self._entries.get(key)
                if e is not None and e.version == ver:
                    self._entries.move_to_end(key)
                    return e
            e = self._build(tenant_id, context_id, ver)
            if e is None:
                return None
            with self._lock:
                old = self._entries.pop(key, None)
                if old is not None:
                    self._total -= old.nbytes
                if e.nbytes <= self.max_bytes:
                    self._entries[key] = e
                    self._total += e.nbytes
                    while self._total > self.max_bytes and len(self._entries) > 1:
                        _, ev = self._entries.popitem(last=False)
                        self._total -= ev.nbytes
            return e

    def _build(self, tenant_id: str, context_id: str, ver) -> Optional[_CtxEntry]:
        # One npz load for metadata+vecs. Vecs stay np.float32 rows — the
        # per-request .astype().tolist() of every vec was pure waste.
        ctx = self.store.get_for_tenant(tenant_id, context_id, load_text=False)
        items = ctx.get("items") or []
        for it in items:
            v = it.get("vec")
            if v is not None:
                it["vec"] = np.asarray(v, dtype=np.float32)
        # Hydrate texts + fit BM25 only under the same caps recall enforces —
        # decompressing every text is what 502'd recall at ~40M words.
        texts = None
        if 0 < len(items) <= HRR_BM25_MAX_ITEMS:
            t = self.store.load_item_texts(
                tenant_id, context_id,
                [it.get("id") for it in items if it.get("id")],
            )
            if sum(len(v or "") for v in t.values()) <= HRR_BM25_MAX_CHARS:
                texts = t
        bm = None
        keep: list = []
        if texts is not None:
            corpus = []
            for it in items:
                s = texts.get(str(it.get("id") or ""))
                if s:
                    corpus.append(s)
                    keep.append(it)
            bm = build_bm25(corpus)  # outside every lock; swapped in below
        if self.store.state_version(context_id) != ver:
            return None  # raced a writer — serve uncached, next lookup rebuilds
        e = _CtxEntry()
        e.version = ver
        e.items = items
        e.texts = texts
        e.bm25 = bm
        e.bm25_items = keep
        # VSA path: the leCore Index is built LAZILY on first vec-path query
        # (BM25-served traffic never pays a forest build — above 4,096 items
        # that build is seconds), single-flight on e.vec_lock, and dies with
        # the entry, so bind/unbind invalidation is the same state-version
        # check the corpus already gets.
        e.vec_index = None
        e.vec_built = False
        e.vec_lock = threading.Lock()
        nb = 0
        if texts is not None:
            for s in texts.values():
                nb += 4 * len(s) + 96  # str object + BM25 postings, rough
        nb += len(items) * (4 * 1024 + 512)  # vec row + item dict overhead
        # Reserve for the lazily-built Index up front: _unit_rows casts to
        # f64, so N×1024d is ~8KB/item. Counting it at build keeps LRU honest
        # even before the first vec query materializes it.
        nb += sum(1 for it in items if it.get("vec") is not None) * 8 * 1024
        e.nbytes = nb
        return e

    def vec_index(self, e: _CtxEntry):
        """(index, by_label) for a cached entry, built at most once per entry.

        MEASURED 2026-08-14 (vsa_profile_bench): rank_stored's per-call
        Index(M, labels) was a per-query HoloForest build above 4,096 items —
        19.6s of a 20.5s query at 100k×1024d vs 2–16ms for a prebuilt
        Index.nearest. Single-flight on the entry's own lock, OUTSIDE the
        store RLock and outside self._lock, so a seconds-long forest build
        never blocks unrelated contexts or store writes. None when the
        context has no usable vecs (rank_stored then fails closed, same as
        uncached)."""
        if not e.vec_built:
            with e.vec_lock:
                if not e.vec_built:
                    e.vec_index = build_vec_index(e.items)
                    e.vec_built = True
        return e.vec_index


class App:
    def __init__(self):
        data = _env("HRR_DATA_DIR", "/workspace/hrr-context/data")
        self.store = ContextStore(data)
        self.cache = ContextCache(self.store)
        self.token = _env("HRR_SERVICE_TOKEN", DEFAULT_TOKEN)

    def auth_ok(self, headers) -> bool:
        h = headers.get("X-HRR-Service-Token") or ""
        if h and h == self.token:
            return True
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:] == self.token:
            return True
        return False


APP = App()


def _json_error(code: str, message: str, http: int = 400) -> Tuple[int, dict]:
    return http, {"ok": False, "error": message, "code": code}


def handle_bind(body: dict) -> Tuple[int, dict]:
    tenant = body.get("tenant_id")
    items = body.get("items")
    mode = body.get("mode") or "append"
    cid = body.get("context_id")

    # chunk=true: let leCore decide the boundaries, not the caller.
    #
    # Callers were slicing bodies into fixed-width windows before posting, and
    # leCore's own chunk_text says why that is wrong in as many words: "WHY NOT
    # FIXED WINDOWS: a fact split across two chunks is retrievable from
    # neither. Paragraphs are the author's own unit of meaning." That is not a
    # style note — MEASURED, a 57-char fact straddling a 1200-char boundary
    # appeared in ZERO of 1,584 chunks, so retrieval was asked to find text
    # that no longer existed anywhere. Chunking is a leCore decision and it
    # belongs on this side of the wire.
    if body.get("chunk") and isinstance(items, list):
        from _lecore import lecore_root  # noqa: E402
        import sys as _sys
        root = lecore_root()
        if root not in _sys.path:
            _sys.path.insert(0, root)
        from holographic.caching_and_storage.holographic_knowledgestore import (  # noqa: E402
            chunk_text,
        )
        max_chars = int(body.get("chunk_max_chars") or 600)
        overlap = int(body.get("chunk_overlap") or 300)

        def _pieces(text: str):
            """leCore's paragraph chunker, with its degenerate path guarded.

            chunk_text is right for prose, and MEASURED here it loses 0 of 86
            needle offsets when the input has real paragraph breaks. But its
            fallback for ONE runaway paragraph is `p[:max_chars]` in a loop —
            fixed windows, the very thing its docstring rejects — and on
            blob input (a pasted log, a NIAH haystack: no blank lines at all)
            that loses 8 of 86 offsets, 9%. So paragraphed text goes to leCore
            untouched, and only the no-paragraph case gets overlapping windows,
            which makes any fact shorter than `overlap` unloseable.
            """
            if "\n\n" in text:
                return chunk_text(text, max_chars=max_chars) or ([text] if text else [])
            stride = max(1, max_chars - min(overlap, max_chars // 2))
            out = []
            for i in range(0, len(text), stride):
                out.append(text[i:i + max_chars])
                if i + max_chars >= len(text):
                    break
            return out or ([text] if text else [])

        expanded = []
        for it in items:
            if not isinstance(it, dict):
                expanded.append(it)
                continue
            text = str(it.get("text") or "")
            for p in _pieces(text):
                nx = dict(it)
                nx.pop("id", None)  # one id per piece, minted by the store
                nx["text"] = p
                expanded.append(nx)
        items = expanded

    out = APP.store.bind(tenant, items, context_id=cid, mode=mode)
    # public façade shape — drop internal minted flag from required fields but keep harmless
    return 200, {
        "object": out["object"],
        "context_id": out["context_id"],
        "bound": out["bound"],
        "item_ids": out["item_ids"],
    }


def handle_unbind(body: dict) -> Tuple[int, dict]:
    out = APP.store.unbind(
        body.get("tenant_id"),
        body.get("context_id"),
        item_ids=body.get("item_ids") or [],
        all_items=bool(body.get("all")),
    )
    return 200, out


def _rank(query: str, items: list, top_k: int, prebuilt=None):
    """Bind-time vectors first. Legacy full-scan only for tiny unindexed
    contexts — on a 99M-word bind the scan misses ATTACH_TIMEOUT and the
    gateway fail-closes 503 hrr_unavailable.

    `prebuilt` = ContextCache.vec_index(entry): skips the per-query
    Index(M, labels) construction (a forest build above 4,096 items)."""
    ranked = rank_stored(query, items, top_k=top_k, prebuilt=prebuilt)
    if ranked is not None:
        return ranked
    nchars = sum(len(it.get("text") or "") for it in items)
    if nchars > 200_000 or len(items) > 16:
        return []
    return rank_items(query, items, top_k=top_k)


def handle_recall(body: dict) -> Tuple[int, dict]:
    tenant = body.get("tenant_id")
    cid = body.get("context_id")
    query = body.get("query") or ""
    top_k = int(body.get("top_k") or 16)
    top_k = max(1, min(128, top_k))
    budget = body.get("budget_tokens")
    # Corpus cache first: one lookup replaces the four state.npz reloads +
    # per-request BM25 refit that made p50 ~394ms while rank was 0.1–0.4ms.
    cached = APP.cache.lookup(tenant, cid)
    if cached is not None:
        items = cached.items
    else:
        items = APP.store.list_items(tenant, cid, load_text=False)

    # BM25 FIRST when the corpus is small enough to hold. MEASURED on MTEB, the
    # dense VSA path is the worst config we ship (SciFact 0.4160 vs 0.6705) and
    # fusing it in degrades monotonically -- so lexical leads and vec is only
    # the fallback.
    #
    # THE GUARD IS LOAD-BEARING, NOT DEFENSIVE. load_text=False exists because
    # decompressing every text is exactly what 502'd recall at ~40M words on
    # 19MB items. The zoo's chunks are ~600 chars, so loading them is cheap; a
    # fat legacy bind is not. Count first, load only under the cap.
    # (The cache honors the same caps at build: bm25 is None on an over-cap
    # corpus, so the cached path falls through to the vec fallback identically.)
    ranked = []
    n_items = len(items)
    if cached is not None:
        if cached.bm25 is not None:
            try:
                ranked = rank_bm25_prebuilt(
                    query, cached.bm25, cached.bm25_items, top_k=max(24, top_k * 8)
                )
            except Exception:
                ranked = []      # fail back to the vec path, never 500
    elif 0 < n_items <= HRR_BM25_MAX_ITEMS:
        try:
            texts = APP.store.load_item_texts(
                tenant, cid, [it.get("id") for it in items if it.get("id")])
            total = sum(len(v or "") for v in texts.values())
            if total <= HRR_BM25_MAX_CHARS:
                hydrated = []
                for it in items:
                    t = texts.get(str(it.get("id") or ""))
                    if t:
                        rec = dict(it)
                        rec["text"] = t
                        hydrated.append(rec)
                ranked = rank_bm25(query, hydrated, top_k=max(24, top_k * 8))
        except Exception:
            ranked = []          # fail back to the vec path, never 500
    if not ranked:
        # Lazily materialize the cached Index only when the vec path actually
        # runs — BM25-served requests above never pay the forest build.
        vec_idx = APP.cache.vec_index(cached) if cached is not None else None
        ranked = _rank(query, items, top_k=max(24, top_k * 8), prebuilt=vec_idx)

    # QUERY NORMALISATION, UNIONED. BM25 scores against every query term, so an
    # exhaustive request ("list every mention of X, with context, be
    # exhaustive") buries X under ten imperative words. MEASURED on the real
    # ranker: 1/3 scattered needles at top_k=16 for the full instruction, 3/3
    # for the content terms alone. We rank the stripped query too and UNION the
    # candidates -- a normalisation that helps adds needles, one that hurts
    # cannot subtract them, and the rewrite is drop-only so it can never drift
    # from what the user asked.
    nq = rank_normalize_query(query) if query else ""
    if nq and nq != (query or "").strip().lower():
        try:
            extra = []
            if cached is not None and cached.bm25 is not None:
                extra = rank_bm25_prebuilt(nq, cached.bm25, cached.bm25_items,
                                           top_k=max(24, top_k * 8))
            if extra:
                # RECIPROCAL RANK FUSION, not max/append-by-score. BM25 scores are
                # sums over query terms, so the long instruction-wrapped query
                # scores higher in ABSOLUTE terms than the stripped one even where
                # it ranks worse — fusing by score lets the diluted query dominate.
                # MEASURED on real BEIR corpora + qrels (query_norm_bench.py,
                # 120 queries each), recall@8 on instruction-wrapped queries:
                #   scifact  0.7894 -> 0.7936 by score   vs 0.8190 by RRF
                #   nfcorpus 0.1290 -> ~0.131 by score   vs 0.1570 by RRF
                # RRF recovers ~82% of the wrapping loss where score-fusion
                # recovered ~12%, and is neutral on bare queries (no harm done).
                RRF_K = 60.0
                fused, byid = {}, {}
                for r, (it, _sc) in enumerate(ranked):
                    key = str(it.get("id") or id(it))
                    byid[key] = it
                    fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + r + 1)
                for r, (it, _sc) in enumerate(extra):
                    key = str(it.get("id") or id(it))
                    byid.setdefault(key, it)
                    fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + r + 1)
                ranked = [(byid[k], v) for k, v in
                          sorted(fused.items(), key=lambda kv: -kv[1])]
        except Exception:
            pass          # union is an improvement, never a dependency
    # SEMANTIC_STAGE (env-gated, default off — zero behavior change when off):
    # quantized-bge second stage. rerank = re-order BM25's top-k; union = a
    # parallel semantic lane whose candidates UNION with BM25's before the
    # budget cut (the J=0 fix — at zero stem overlap BM25 returns nothing
    # rankable). NOT the dead dense-VSA arm; no score fusion (measured:
    # fusing dense into BM25 degraded monotonically). When the stage is ON
    # and broken it raises — a silent BM25 fallback would poison before/after.
    if semantic_stage.mode() != "off" and query:
        if cached is not None and cached.texts is not None:
            _ct = cached.texts

            def _texts_of(ids, _t=_ct):
                return {str(i): _t.get(str(i)) or "" for i in ids}
        else:
            def _texts_of(ids):
                return APP.store.load_item_texts(tenant, cid, ids)
        ranked = semantic_stage.apply_stage(
            query, ranked, items, _texts_of,
            cache_key=(tenant, cid, APP.store.state_version(cid)),
            top_k=max(24, top_k * 8),
        )
    # Drop Telegram HTML shells without inflating them. Paths were not
    # persisted on early binds; peek the [file:] / doctype prefix instead.
    peek_ids = [it.get("id") for it, _ in ranked if it.get("id")]
    if cached is not None and cached.texts is not None:
        peek = {str(i): (cached.texts.get(str(i)) or "")[:240] for i in peek_ids}
    else:
        peek = APP.store.peek_prefixes(tenant, cid, peek_ids)
    kept = []
    for it, score in ranked:
        path = str((it.get("metadata") or {}).get("path") or peek.get(str(it.get("id") or ""), "")).lower()
        pref = peek.get(str(it.get("id") or ""), "")
        if path.endswith((".html", ".htm", ".css", ".xml")):
            continue
        if "<!doctype" in pref.lower() or pref.lstrip().startswith("<html"):
            continue
        if "messages.html" in pref.lower() or "/chats/chat_" in pref.lower():
            continue
        if ".app/" in pref.lower() or "discordchatexporter" in pref.lower():
            continue
        kept.append((it, score))
        if len(kept) >= top_k:
            break
    ranked = kept
    kept_ids = [it.get("id") for it, _ in ranked if it.get("id")]
    if cached is not None and cached.texts is not None:
        texts = {str(i): cached.texts.get(str(i)) or "" for i in kept_ids}
    else:
        texts = APP.store.load_item_texts(tenant, cid, kept_ids)
    out_items = []
    used = 0
    for it, score in ranked:
        text = texts.get(str(it.get("id") or "")) or it.get("text") or ""
        if budget is not None:
            try:
                b = int(budget)
            except (TypeError, ValueError):
                raise StoreError("invalid_request", "budget_tokens must be int", 400)
            est = estimate_tokens(text)
            if used + est > b and out_items:
                break
            if est > b:
                chars = max(16, b * 4)
                text = _center_window(text, query, chars)
                est = estimate_tokens(text)
            used += est
        out_items.append(
            {
                "id": it.get("id"),
                "text": text,
                "score": round(float(score), 6),
                "metadata": it.get("metadata") or {},
            }
        )
    # CORPUS SIZE, so a caller can price the counterfactual.
    #
    # The zoo gateway bills an attach call against "what answering this without
    # leCore would have cost", which is shipping the whole bound corpus. It had
    # no way to know that size: recall returned only the slice, so the gateway
    # fell back to pre-recall prompt tokens -- which for attach is SMALLER than
    # what it forwards (the ask is tiny, the recalled slice is added), so the
    # counterfactual never applied and every attach call reported a saving of
    # exactly 1/markup. Both numbers below are already computed above, so this
    # costs nothing. corpus_chars is None when texts were never hydrated (a
    # corpus over the BM25 caps); chunks is always real.
    corpus_chars = None
    if cached is not None and cached.texts is not None:
        corpus_chars = sum(len(v or "") for v in cached.texts.values())
    return 200, {
        "object": "hrr.recall",
        "context_id": cid,
        "items": out_items,
        "chunks": n_items,
        "corpus_chars": corpus_chars,
    }


def _query_from_messages(messages) -> str:
    if not isinstance(messages, list):
        return ""
    users = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                users.append(c)
            elif isinstance(c, list):
                # OpenAI multi-part — take text bits
                users.append(
                    " ".join(
                        p.get("text", "")
                        for p in c
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                )
    if users:
        return users[-1]
    # fallback: concat all string contents
    bits = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            bits.append(m["content"])
    return "\n".join(bits)


def _center_window(text: str, query: str, max_chars: int) -> str:
    """Keep a max_chars window centered on the query's rarest matching term, so a
    needle at the END/middle of a long chunk survives budget truncation."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    low = text.lower()
    best_pos = -1
    best_count = None
    for tok in tokenize(query):
        n = len(tok)
        first = -1
        count = 0
        start = 0
        while True:
            pos = low.find(tok, start)
            if pos < 0:
                break
            before = pos == 0 or not low[pos - 1].isalnum()
            after = pos + n >= len(low) or not low[pos + n].isalnum()
            if before and after:
                count += 1
                if first < 0:
                    first = pos
            start = pos + n
        if first < 0:
            continue
        if best_count is None or count < best_count:
            best_count = count
            best_pos = first
    if best_pos < 0:
        return text[:max_chars]
    half = max_chars // 2
    start = best_pos - half
    if start < 0:
        start = 0
    end = start + max_chars
    if end > len(text):
        end = len(text)
        start = max(0, end - max_chars)
    return text[start:end]


def handle_attach(body: dict) -> Tuple[int, dict]:
    tenant = body.get("tenant_id")
    cid = body.get("context_id")
    messages = body.get("messages")
    if not tenant:
        raise StoreError("invalid_request", "tenant_id required", 400)
    if not cid:
        raise StoreError("invalid_request", "context_id required", 400)
    if messages is None:
        raise StoreError("invalid_request", "messages required", 400)
    budget = body.get("budget_tokens")
    if budget is None:
        budget = DEFAULT_BUDGET
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        raise StoreError("invalid_request", "budget_tokens must be int", 400)
    budget = max(0, budget)

    cached = APP.cache.lookup(tenant, cid)
    if cached is not None:
        items = cached.items
    else:
        items = APP.store.list_items(tenant, cid, load_text=False)
    if not items:
        return 200, {
            "object": "hrr.attach",
            "context_id": cid,
            "inject": {
                "mode": "messages_prefix",
                "messages": [],
                "item_ids": [],
                "tokens_est": 0,
            },
            "usage": {"recalled": 0, "budget_tokens": budget},
        }

    query = _query_from_messages(messages)
    vec_idx = APP.cache.vec_index(cached) if cached is not None else None
    ranked = _rank(query, items, top_k=min(16, 128), prebuilt=vec_idx)
    ranked_ids = [it.get("id") for it, _ in ranked if it.get("id")]
    if cached is not None and cached.texts is not None:
        texts = {str(i): cached.texts.get(str(i)) or "" for i in ranked_ids}
    else:
        texts = APP.store.load_item_texts(tenant, cid, ranked_ids)
    # Pack into one system message under budget
    header = (
        "HRR external context (selected memory for this turn; "
        "not a larger native window):\n"
    )
    used = estimate_tokens(header)
    chosen_ids = []
    parts = []
    for it, score in ranked:
        text = texts.get(str(it.get("id") or "")) or it.get("text") or ""
        block = f"- [{it.get('id')}] {text}\n"
        est = estimate_tokens(block)
        if used + est > budget:
            # query-aware truncation: keep a window centered on the query's rarest
            # term so a needle at the END of a long chunk is not lost to front-cut.
            if not parts and budget > used + 8:
                remain = (budget - used) * 4
                win = _center_window(text, query, remain)
                trunc = f"- [{it.get('id')}] {win}\n"
                parts.append(trunc)
                chosen_ids.append(it.get("id"))
                used += estimate_tokens(trunc)
            break
        parts.append(block)
        chosen_ids.append(it.get("id"))
        used += est

    inject_msgs = []
    if parts:
        inject_msgs = [{"role": "system", "content": header + "".join(parts)}]

    return 200, {
        "object": "hrr.attach",
        "context_id": cid,
        "inject": {
            "mode": "messages_prefix",
            "messages": inject_msgs,
            "item_ids": chosen_ids,
            "tokens_est": used if parts else 0,
        },
        "usage": {"recalled": len(chosen_ids), "budget_tokens": budget},
    }



# ---------------------------------------------------------------------------
# leCore's OWN faculties, exposed verbatim — docs/ZOO.md §1, §9, §11.
#
# Rule-0 for the whole zoo: before a model (or we) hand-roll an algorithm,
# `find` asks the live catalog whether leCore already has it. `invoke` runs any
# of them through leCore's own dispatch, so there is exactly one implementation
# and the private-faculty gate is inherited rather than re-enforced here.
# Every response carries measured cost + a determinism receipt (§4, §11.1).

def handle_lecore_find(body: dict) -> Tuple[int, dict]:
    import lecore_bridge  # noqa: E402
    q = body.get("query")
    if not isinstance(q, str) or not q.strip():
        return 400, {"ok": False, "error": "query (string) required", "code": "invalid_request"}
    return 200, lecore_bridge.find(q, top=int(body.get("top") or 8))


def handle_lecore_describe(body: dict) -> Tuple[int, dict]:
    import lecore_bridge  # noqa: E402
    n = body.get("name")
    if not isinstance(n, str) or not n.strip():
        return 400, {"ok": False, "error": "name (string) required", "code": "invalid_request"}
    return 200, lecore_bridge.describe(n)


def handle_lecore_invoke(body: dict) -> Tuple[int, dict]:
    import lecore_bridge  # noqa: E402
    n = body.get("name")
    if not isinstance(n, str) or not n.strip():
        return 400, {"ok": False, "error": "name (string) required", "code": "invalid_request"}
    try:
        return 200, lecore_bridge.invoke(n, body.get("args") or {})
    except Exception as e:  # a refused/unknown faculty is a 400, not a 500
        return 400, {"ok": False, "error": str(e)[:300], "code": "invoke_failed"}


def handle_memory_write(body: dict) -> Tuple[int, dict]:
    """OUROBOROS mouth — write a fact into the tenant's durable memory partition.
    See ouroboros.py; the model's external memory, managed server-side."""
    import ouroboros  # noqa: E402  (imports only rank, already loaded)
    tenant = body.get("tenant_id")
    text = body.get("text")
    tags = body.get("tags") or []
    out = ouroboros.memory_write(tenant, text, tags=tags)
    if isinstance(out, dict) and out.get("error"):
        return 400, {"ok": False, "error": out["error"], "code": "invalid_request"}
    return 200, {"object": "ouroboros.memory_write", **out}


def handle_memory_search(body: dict) -> Tuple[int, dict]:
    """OUROBOROS read — ranked recall over the tenant's durable memory partition."""
    import ouroboros  # noqa: E402
    tenant = body.get("tenant_id")
    query = body.get("query")
    top = int(body.get("top") or 4)
    tags = body.get("tags") or None
    out = ouroboros.memory_search(tenant, query, top=top, tags=tags)
    if isinstance(out, dict) and out.get("error"):
        return 400, {"ok": False, "error": out["error"], "code": "invalid_request"}
    return 200, {"object": "ouroboros.memory_search", **out}


GATE_MAX_ITEMS = int(os.environ.get("HRR_GATE_MAX_ITEMS") or 400)
GATE_MAX_CHARS = int(os.environ.get("HRR_GATE_MAX_CHARS") or 400_000)

# ---------------------------------------------------------------- delta bind

def _chunk_dir(tenant: str) -> str:
    # TENANT-SCOPED. A global content-addressed store would make the probe an
    # existence oracle: tenant B could learn whether tenant A holds a chunk by
    # hashing a guess and asking. Identical text is stored twice across
    # tenants; that duplication is the price of not leaking membership.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(tenant or "anon"))[:80]
    d = os.path.join(_env("HRR_DATA_DIR", "/workspace/hrr-context/data"), "chunks", safe)
    os.makedirs(d, exist_ok=True)
    return d


def _chunk_path(tenant: str, h: str) -> str:
    return os.path.join(_chunk_dir(tenant), h[:2], h)


def _chunk_have(tenant: str, h: str) -> bool:
    return os.path.isfile(_chunk_path(tenant, h))


def _chunk_put(tenant: str, h: str, text: str) -> None:
    p = _chunk_path(tenant, h)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)                       # atomic, like the store's own writes


def _chunk_get(tenant: str, h: str) -> Optional[str]:
    try:
        with open(_chunk_path(tenant, h), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def handle_delta(body: dict) -> Tuple[int, dict]:
    """CHUNK-LEVEL DELTA BIND — the rsync move at the corpus seam.

    The bind path content-addresses at the CORPUS level: openzoo-shim
    concatenates every file into one string, hashes THAT, and on a miss
    re-uploads every part. So editing one file in a repo re-ships the whole
    tree. Its own comment measures the cost: a 23MB Rust tree ran 25s -> 30s ->
    65s in 4MB parts, and a deploy mid-bind threw five good parts away.

    Two phases, per leCore's spec:
      PROBE  {chunk_hashes:[sha256 hex,...]}          -> {missing, known}
      FILL   {chunk_hashes:[...], chunks:{hash:text}} -> ships only the missing

    When every hash resolves, the corpus assembles IN HASH-LIST ORDER and binds
    under a normal context_id, so everything downstream (recall, gate, ask) is
    identical to a whole bind. A delta bind and a whole bind are
    indistinguishable after the fact, which is the property that makes this safe
    to put in front of the existing path.

    MIS-KEYED CHUNKS ARE REFUSED PER-CHUNK, not per-request: a caller that
    mislabels one chunk gets that one rejected and keeps the rest of its upload.
    Content addressing is only a safety property if the address is VERIFIED —
    storing text under a hash we did not recompute would let one bad client
    poison a chunk every other corpus then reuses by hash.
    """
    tenant = body.get("tenant_id")
    hashes = body.get("chunk_hashes")
    if not isinstance(hashes, list) or not hashes:
        return 400, {"ok": False, "code": "invalid_request",
                     "error": "chunk_hashes must be a non-empty list"}
    hashes = [str(h).lower().strip() for h in hashes]
    bad = [h for h in hashes if len(h) != 64 or any(c not in "0123456789abcdef" for c in h)]
    if bad:
        return 400, {"ok": False, "code": "invalid_request",
                     "error": f"not sha256 hex: {bad[:3]}"}

    stored, refused = [], []
    chunks = body.get("chunks")
    if isinstance(chunks, dict):
        wanted = set(hashes)
        for h, text in chunks.items():
            h = str(h).lower().strip()
            if h not in wanted:
                refused.append({"hash": h[:16], "why": "not in chunk_hashes"})
                continue
            if not isinstance(text, str):
                refused.append({"hash": h[:16], "why": "text must be a string"})
                continue
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if actual != h:
                refused.append({"hash": h[:16], "why": f"content hashes to {actual[:16]}"})
                continue
            _chunk_put(tenant, h, text)
            stored.append(h)

    missing = [h for h in hashes if not _chunk_have(tenant, h)]
    known = [h for h in hashes if _chunk_have(tenant, h)]
    out = {
        "object": "hrr.delta",
        "missing": missing,
        "known": known,
        "stored": len(stored),
        "refused": refused,
        "complete": not missing,
        "shipped_bytes": sum(len((chunks or {}).get(h) or "") for h in stored),
    }
    if missing:
        return 200, out                       # probe, or a partial fill

    # Every hash resolves: assemble and bind. Order is the CALLER's hash list,
    # never the order chunks happened to arrive in.
    texts = []
    for h in hashes:
        t = _chunk_get(tenant, h)
        if t is None:                         # evicted between the check and now
            out["missing"] = [h]
            out["complete"] = False
            return 200, out
        texts.append(t)
    # THROUGH handle_bind, NOT the store directly. The first version called
    # APP.store.bind on the joined text, which skips leCore chunking and lands
    # the whole corpus as ONE item — recall then has a single giant chunk to
    # rank, which is exactly the fixed-window failure chunk=true exists to
    # avoid. A delta bind must be indistinguishable downstream from a whole
    # bind, so it takes the identical path, chunk params included.
    joined = "\n\n".join(texts)
    bind_body = {"tenant_id": tenant, "items": [{"text": joined}], "chunk": True}
    for k in ("chunk_max_chars", "chunk_overlap"):
        if body.get(k) is not None:
            bind_body[k] = body[k]
    code, res = handle_bind(bind_body)
    if code != 200:
        return code, res
    out["context_id"] = res.get("context_id")
    out["bound"] = res.get("bound")
    out["chars"] = len(joined)
    return 200, out


def handle_gate(body: dict) -> Tuple[int, dict]:
    """THE PRE-PAYMENT GATE: can this corpus support this ask, before we quote?

    Runs leCore's adaptive retrieval cascade (corpus_gate -> retrieval_dispatch)
    over a bound context and returns a flat verdict a gateway can act on BEFORE
    a 402. answerable=False is a CERTIFIED abstain -- the cascade decided the
    corpus cannot support the ask -- not merely a low score.

    KEPT NEGATIVE, and it is the whole reason this is safe to ship: an abstain
    says the CORPUS cannot answer. The MODEL still might, from its own weights.
    So this gates the corpus-grounded price tier and never the model's
    existence. A caller that refuses outright on `answerable:false` is using
    this wrongly.

    FAILS OPEN, ALWAYS. This sits in front of revenue: every error path returns
    answerable=true with a stated reason, because a gate that fail-closes turns
    an internal fault into a refused, unbilled request. The only thing that ever
    returns False here is the cascade itself, deliberately.
    """
    tenant = body.get("tenant_id")
    cid = body.get("context_id")
    query = body.get("query") or ""
    if not cid or not query:
        return 400, {"ok": False, "code": "invalid_request",
                     "error": "context_id and query are required"}
    k = max(1, min(64, int(body.get("k") or 5)))
    tau = float(body.get("tau") or 0.25)

    try:
        # GATE THE DOCS RECALL WOULD ACTUALLY SERVE, NOT A SLICE OF THE BIND.
        #
        # The first version fed corpus_gate the first N chunks in BIND ORDER.
        # On an 8M-char context that is an arbitrary prefix, and MEASURED against
        # this session's own transcript it passed "what is the mating ritual of
        # the emperor penguin" as answerable -- a false pass, because the
        # cascade was asked about a corpus nobody would ever retrieve.
        #
        # leCore's corpus_gate delegates to retrieval_dispatch precisely "so the
        # verdict and the retrieval never disagree". Handing it a different doc
        # set than recall uses breaks the one property it is built to hold. So
        # recall runs first and the gate judges its candidates: if the
        # best-ranked docs cannot support the ask, nothing in the corpus can.
        rc, rbody = handle_recall({
            "tenant_id": tenant, "context_id": cid, "query": query,
            "top_k": max(16, min(64, k * 8)),
        })
        if rc != 200:
            return 200, {"object": "hrr.gate", "answerable": True, "stage": "recall_failed",
                         "margin": 0.0, "docs_seen": 0, "truncated": False,
                         "advice": "forward: could not rank the corpus to gate it"}
        ranked_items = (rbody or {}).get("items") or []
        truncated = False
        docs, chars = [], 0
        for it in ranked_items[:GATE_MAX_ITEMS]:
            t = it.get("text")
            if not t:
                continue
            if chars + len(t) > GATE_MAX_CHARS:
                truncated = True
                break
            docs.append(t)
            chars += len(t)
        if not docs:
            return 200, {"object": "hrr.gate", "answerable": True, "stage": "no_corpus",
                         "margin": 0.0, "docs_seen": 0, "truncated": False,
                         "advice": "forward: nothing bound to gate against"}

        import lecore_bridge as _lb
        out = _lb.invoke("corpus_gate", {"query": query, "docs": docs, "k": k, "tau": tau})
        res = out.get("result")
        payload = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else {}
        g = (payload or {}).get("result") or {}
        if "answerable" not in g:
            # Faculty missing (an older leCore) or a shape we do not recognise.
            return 200, {"object": "hrr.gate", "answerable": True, "stage": "unavailable",
                         "margin": 0.0, "docs_seen": len(docs), "truncated": truncated,
                         "advice": "forward: gate unavailable on this leCore build",
                         "detail": str((payload or {}).get("error") or "")[:200]}
        return 200, {
            "object": "hrr.gate",
            "answerable": bool(g.get("answerable")),
            "stage": g.get("stage"),
            "margin": float(g.get("margin") or 0.0),
            "ranked": g.get("ranked") or [],
            "advice": g.get("advice"),
            "docs_seen": len(docs),
            "truncated": truncated,
            "receipt": out.get("receipt"),
        }
    except Exception as e:                                   # noqa: BLE001
        return 200, {"object": "hrr.gate", "answerable": True, "stage": "error",
                     "margin": 0.0, "docs_seen": 0, "truncated": False,
                     "advice": "forward: gate errored, never block on our fault",
                     "detail": f"{type(e).__name__}: {str(e)[:160]}"}


ROUTES = {
    ("GET", "/health"): lambda _b: (
        200,
        {
            "ok": True,
            "name": "hrr-context",
            "version": VERSION,
            "routes": [
                "GET /health",
                "POST /internal/v1/hrr/bind",
                "POST /internal/v1/hrr/unbind",
                "POST /internal/v1/hrr/recall",
                "POST /internal/v1/hrr/gate",
                "POST /internal/v1/hrr/delta",
                "POST /internal/v1/hrr/attach",
                "POST /internal/v1/memory/write",
                "POST /internal/v1/memory/search",
            ],
        },
    ),
    ("POST", "/internal/v1/hrr/bind"): handle_bind,
    ("POST", "/internal/v1/hrr/unbind"): handle_unbind,
    ("POST", "/internal/v1/hrr/recall"): handle_recall,
    ("POST", "/internal/v1/hrr/gate"): handle_gate,
    ("POST", "/internal/v1/hrr/delta"): handle_delta,
    ("POST", "/internal/v1/hrr/attach"): handle_attach,
    ("POST", "/internal/v1/lecore/find"): handle_lecore_find,
    ("POST", "/internal/v1/lecore/describe"): handle_lecore_describe,
    ("POST", "/internal/v1/lecore/invoke"): handle_lecore_invoke,
    ("POST", "/internal/v1/memory/write"): handle_memory_write,
    ("POST", "/internal/v1/memory/search"): handle_memory_search,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "HRRContext/" + VERSION

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise StoreError("invalid_request", "body too large", 413)
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise StoreError("invalid_request", "body must be JSON", 400)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        path = urlparse(self.path).path
        key = (method, path)
        if key not in ROUTES:
            self._send(404, {"ok": False, "error": "no such endpoint", "code": "not_found"})
            return
        if path != "/health" and not APP.auth_ok(self.headers):
            self._send(401, {"ok": False, "error": "unauthorized", "code": "unauthorized"})
            return
        try:
            body = self._read_json() if method == "POST" else {}
            code, payload = ROUTES[key](body)
            self._send(code, payload)
        except StoreError as e:
            self._send(e.http, {"ok": False, "error": e.message, "code": e.code})
        except Exception as e:
            self._send(
                500,
                {
                    "ok": False,
                    "error": "%s: %s" % (type(e).__name__, e),
                    "code": "server_error",
                },
            )


def main():
    host = _env("HRR_HOST", "127.0.0.1")
    port = int(_env("HRR_PORT", str(DEFAULT_PORT)))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(
        "HRR Context dogfood listening on http://%s:%d  token=%s  data=%s"
        % (host, port, APP.token, APP.store.root),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
