"""Ranker on the CAPABILITIES.md memory homes — not a lookalike.

encode_record + bind(CONTENT, phrase) at write. Index.nearest at read.
pack/score_all is the superposition readout. unbind+decode_record recovers
the item id. Ties via argmax_tiebreak (ISA-1).
"""
import math
import re
import os
from collections import Counter

import numpy as np

from _lecore import import_memory

_M = import_memory()
Vocabulary = _M["Vocabulary"]
bind = _M["bind"]
bundle = _M["bundle"]
cosine = _M["cosine"]
unbind = _M["unbind"]
Index = _M["Index"]
ShapeVocab = _M["ShapeVocab"]
encode_record = _M["encode_record"]
decode_record = _M["decode_record"]
pack = _M["pack"]
score_all = _M["score_all"]
argmax_tiebreak = _M["argmax_tiebreak"]

DIM = int(os.environ.get("HRR_DIM") or "1024")
_PUNCT = ".,;:!?()[]{}'\"`~@#$%^&*_=+<>|/\\"
_TRANS = str.maketrans(_PUNCT, " " * len(_PUNCT))
_TOKS = Vocabulary(DIM, seed=0, derived=True)
_SHAPE = ShapeVocab(DIM, seed=2)
_CONTENT = _SHAPE.role("CONTENT")
_NAME = _SHAPE.role("NAME")
_CODE = _SHAPE.role("CODE")
_LEAF_K = min(102, max(8, DIM // 10))  # ≤ 0.1·D — Fable-5 Vertex
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "it", "as", "at", "by", "be", "this", "that", "what", "which",
    "was", "are", "from", "have", "has", "had", "not", "but", "if",
    "answer", "only", "number", "please", "how", "does", "did",
    # Telegram / browser export chrome — same collapse as kind:passage
    "html", "htm", "head", "body", "meta", "div", "span", "href", "src",
    "doctype", "charset", "stylesheet", "onclick", "script", "style",
    "class", "http", "https", "www", "com", "file", "message", "messages",
    "chat", "chats", "dataexport", "utf",
}
_JUNK_SUF = (".html", ".htm", ".css", ".xml")


def tokenize(text: str):
    return (text or "").lower().translate(_TRANS).split()


def _tok_weight(tok: str, count: int, df: dict | None, n_docs: int) -> float:
    w = math.log1p(count)
    if df is not None and n_docs:
        dft = int(df.get(tok, 1))
        if dft >= 0.9 * n_docs:
            return 0.0
        w *= math.log(n_docs / max(1, dft))
    return w


def _phrase(text: str, df: dict | None = None, n_docs: int = 0) -> np.ndarray:
    """Top-_LEAF_K IDF-weighted atoms. k ≤ 0.1·D so one term is ~0.1 cosine."""
    cnt = Counter(tokenize(text))
    if not cnt:
        return np.zeros(DIM, dtype=float)
    scored = []
    for tok, c in cnt.items():
        w = _tok_weight(tok, c, df, n_docs)
        if w > 0.0:
            scored.append((w, tok))
    scored.sort(reverse=True)
    scored = scored[:_LEAF_K]
    if not scored:
        return np.zeros(DIM, dtype=float)
    return bundle([w * _TOKS.get(tok) for w, tok in scored])


def _roles(text: str, df: dict | None = None, n_docs: int = 0) -> np.ndarray:
    """bind(NAME, alpha) / bind(CODE, digits) so a name-only query can hit."""
    parts = []
    for tok, c in Counter(tokenize(text)).items():
        if tok in _STOP or len(tok) < 3:
            continue
        w = _tok_weight(tok, c, df, n_docs)
        if w <= 0.0:
            continue
        atom = _TOKS.get(tok)
        if tok.isdigit():
            parts.append(bind(_CODE, atom) * w)
        elif tok.isalpha() and len(tok) >= 4:
            parts.append(bind(_NAME, atom) * w)
    return bundle(parts) if parts else np.zeros(DIM, dtype=float)


def _record(text: str, iid: str = "", df: dict | None = None, n_docs: int = 0) -> np.ndarray:
    del iid
    phrase = _phrase(text, df=df, n_docs=n_docs)
    roles = _roles(text, df=df, n_docs=n_docs)
    bits = [bind(_CONTENT, phrase)]
    if float(np.linalg.norm(roles)):
        bits.append(roles)
    return bundle(bits)


def _unit_rows(M: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n


def estimate_tokens(text: str) -> int:
    return max(1, (len(text or "") + 3) // 4) if text else 0


def item_vector(text: str, iid: str = "") -> list:
    """Bind-time record: bind(CONTENT, phrase). No shared kind atom."""
    v = np.asarray(_record(text, iid), dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n:
        v /= n
    return v.tolist()


# Fable-5: ~80-token leaf (~512 chars) stays under 0.1*DIM capacity.
_CHUNK = int(os.environ.get("HRR_CHUNK_CHARS") or "512")
_MAX_CHUNKS = int(os.environ.get("HRR_MAX_CHUNKS") or "8")


def _token_positions(text: str, tok: str, limit: int = 8):
    """Word-boundary hits for one token. Cap the scan — do not walk every 'the'."""
    hits = []
    if not text or len(tok) < 3:
        return hits
    ntext = len(text)
    for var in (tok, tok.capitalize(), tok.upper()):
        start = 0
        n = len(var)
        while len(hits) < limit:
            pos = text.find(var, start)
            if pos < 0:
                break
            before = pos == 0 or not text[pos - 1].isalnum()
            after = pos + n >= ntext or not text[pos + n].isalnum()
            if before and after:
                hits.append(pos)
            start = pos + n
        if len(hits) >= limit:
            break
    return hits


def _content_toks(query: str):
    return [t for t in tokenize(query) if t not in _STOP and len(t) >= 4]


def _df_texts(texts, toks):
    """Document frequency over a list of strings (leaves, not 19MB items)."""
    df = {t: 0 for t in toks}
    for text in texts:
        if not text:
            continue
        for t in toks:
            if _token_positions(text, t, limit=1):
                df[t] += 1
    return df


_TEMPLATE = {"vault", "access", "code", "project"}


def _query_chunks(text: str, toks, width: int = _CHUNK, cap: int = _MAX_CHUNKS):
    """One 512-char leaf per query token (rare tokens first). A global
    cap used to fill with vault/project and never reach lecore."""
    if not text or not toks:
        return []
    ordered = sorted(set(toks), key=lambda t: (t in _TEMPLATE, -len(t)))
    used, out = [], []
    half = width // 2
    n = len(text)
    for tok in ordered:
        for pos in _token_positions(text, tok, limit=2):
            if any(abs(pos - u) < half for u in used):
                continue
            used.append(pos)
            a = max(0, pos - half)
            b = min(n, a + width)
            a = max(0, b - width)
            out.append(text[a:b])
            break
        if len(out) >= cap:
            break
    return out


# RECALL BUDGET (leCore 0.2.15 "recall-budgeted vector index").
#
# The forest is an APPROXIMATE structure whose recall DEGRADES with N — measured
# upstream on unstructured data: recall@1 vs exact is 0.93 at 5k, 0.59 at 50k,
# 0.51 at 200k. Silently answering half-right at scale is the opposite of what
# this service claims, so we hand Index a budget: it measures recall ON THIS DATA
# at first forest use and falls back to the exact scan when the measurement comes
# in under budget, recording why in `index.recall_note`.
#
# 0.95 is deliberately strict — retrieval quality is the product. Override with
# HRR_RECALL_BUDGET; set it empty to restore leCore's unbudgeted default.
def _recall_budget():
    raw = os.environ.get("HRR_RECALL_BUDGET", "0.95").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.95


def _new_index(M, labels):
    """Index(...) with the recall budget applied, tolerating older leCore.

    A vendored tree without recall_budget must keep working rather than crash the
    ranker — this service fails closed on a MISSING Index, not on an older one.
    """
    budget = _recall_budget()
    if budget is None:
        return Index(M, labels=labels)
    try:
        return Index(M, labels=labels, recall_budget=budget)
    except TypeError:
        return Index(M, labels=labels)


def _index_hits(q: np.ndarray, vecs: list, labels: list, top_k: int):
    """CAPABILITIES.md Index (search): Index(vectors, labels).nearest(q, k).

    No homemade cosine loop. No mean-center. No pack/score_all stand-in.
    If packaged Index is missing, fail closed — do not invent a ranker.
    """
    if Index is None:
        raise RuntimeError("leCore Index missing — refuse homemade rank")
    M = np.vstack(vecs).astype(np.float32)
    q = np.asarray(q, dtype=np.float32)
    if float(np.linalg.norm(q)) == 0.0:
        return []
    return _new_index(M, labels).nearest(q, k=max(1, top_k))


def rank_bm25(query: str, items: list, top_k: int = 8):
    """leCore's own Okapi BM25 over the item texts.

    WHY THIS EXISTS AND WHY IT IS FIRST: MEASURED on MTEB, the hand-rolled dense
    VSA path below is the WORST of the three configurations we shipped --
    SciFact nDCG@10 0.4160 dense-only against 0.6705 for this, and fusing the
    two degrades monotonically at every weight tested (0.05 -> 0.6596,
    0.15 -> 0.6307, 0.30 -> 0.6057). Across four MTEB retrieval tasks BM25 lands
    at published-BM25 parity (SciFact 0.6705 / ~0.665, SCIDOCS 0.1577 / ~0.158,
    NFCorpus 0.3167 / ~0.325) and beats it on ArguAna (0.4314 / ~0.315). The
    dense arm was not a weaker complement, it was a drag.

    Returns [] when texts are absent, so the caller keeps its vec fallback --
    that path exists because loading every text is what 502'd recall at ~40M
    words on 19MB items, and this must not reintroduce it.
    """
    texts, keep = [], []
    for it in items:
        t = it.get("text") or ""
        if not t:
            continue
        texts.append(t)
        keep.append(it)
    if not texts:
        return []
    try:
        from holographic.semantic_router.holographic_bm25 import BM25
    except Exception:
        return []
    bm = BM25(texts)
    out = []
    # expand=True: derivational siblings at HALF weight, exact match still
    # dominates. Pure recall channel -- adds candidates, never removes.
    for idx, score in bm.rank(query, top=max(1, top_k), expand=True):
        if float(score) <= 0.0:
            continue
        out.append((keep[int(idx)], float(score)))
    return out


def build_bm25(texts: list):
    """Prebuild leCore's BM25 over an already-hydrated corpus.

    Same index rank_bm25 constructs per call; the server's corpus cache holds
    it per (tenant, context, store-version) so recall stops paying the rebuild
    (~all of the 394ms p50 at 600 chunks was reload + rebuild, not ranking).
    BM25.rank is read-only after fit, so one instance is thread-safe to share.
    Returns None when the corpus is empty or leCore's BM25 is unavailable.
    """
    if not texts:
        return None
    try:
        from holographic.semantic_router.holographic_bm25 import BM25
    except Exception:
        return None
    return BM25(texts)


def rank_bm25_prebuilt(query: str, bm, keep: list, top_k: int = 8):
    """rank_bm25's scoring loop against a prebuilt index. keep[i] MUST be the
    item whose text was texts[i] at build_bm25 time (same order, same filter)."""
    if bm is None or not keep:
        return []
    out = []
    # expand=True: derivational siblings at HALF weight — same knob as rank_bm25.
    for idx, score in bm.rank(query, top=max(1, top_k), expand=True):
        if float(score) <= 0.0:
            continue
        out.append((keep[int(idx)], float(score)))
    return out


def build_vec_index(items: list):
    """Prebuild the leCore Index over bind-time item vecs.

    EXACTLY the filter + Index(M, labels) construction rank_stored performed
    per call — moved here so the server's corpus cache can hold the result per
    (tenant, context, store-version), the same split build_bm25 is to
    rank_bm25. MEASURED 2026-08-14 (vsa_profile_bench): above the 4,096-item
    forest_threshold that per-call construction is a per-query HoloForest
    build — 19.6s of a 20.5s query at 100k×1024d — while a prebuilt
    Index.nearest costs 2–16ms. nearest() at k>1 is a read-only exact scan
    (no lazy state unless abstain is passed, which we never do), so one
    instance is thread-safe to share.

    Returns (index, by_label) or None when no item carries a usable vec —
    callers keep rank_stored's fail-closed contract on None.
    """
    indexed = []
    for i, it in enumerate(items):
        v = it.get("vec")
        # ndarray-safe: cached items carry vecs as np.float32 rows (no
        # per-request tolist), and `not ndarray` raises on truthiness.
        if v is None or len(v) == 0:
            continue
        arr = np.asarray(v, dtype=np.float32)
        if arr.size != DIM or float(np.linalg.norm(arr)) == 0.0:
            continue
        indexed.append((it, arr, it.get("id") or str(i)))
    if not indexed:
        return None
    if Index is None:
        raise RuntimeError("leCore Index missing — refuse homemade rank")
    M = np.vstack([a for _, a, _ in indexed]).astype(np.float32)
    labels = [lab for _, _, lab in indexed]
    by_lab = {lab: it for it, _, lab in indexed}
    return _new_index(M, labels), by_lab


def rank_stored(query: str, items: list, top_k: int = 8, prebuilt=None):
    """Query via Index.nearest.

    Prefer bind-time item vecs (no text). Loading every zlib blob to cut
    512-char leaves is what 502'd recall at ~40M words. Leaf scan is only
    for tiny unindexed contexts that still have text in memory.

    `prebuilt` is a (index, by_label) pair from build_vec_index over these
    same items — the cache-facing split, like rank_bm25_prebuilt. Same ids
    AND scores as the build-per-call path by construction.
    """
    q = np.asarray(_record(query), dtype=np.float32)
    if float(np.linalg.norm(q)) == 0.0:
        return []
    if prebuilt is not None:
        index, by_lab = prebuilt
    else:
        built = build_vec_index(items)
        if built is None:
            # No bind-time vecs → fail closed. The leaf-text scan 502'd RunPod
            # at ~40M words. Do not rebuild a cosine ranker from bodies.
            return []
        index, by_lab = built
    # Over-ask so HTML export shells can be dropped without a text scan.
    hits = index.nearest(q, k=max(1, max(16, top_k * 8)))
    out = []
    seen = set()
    for lab, score in hits:
        if float(score) <= 0.0:
            continue
        it = by_lab.get(lab)
        if it is None:
            continue
        path = str((it.get("metadata") or {}).get("path") or "").lower()
        if path.endswith(_JUNK_SUF):
            continue
        iid = it.get("id")
        if iid in seen:
            continue
        seen.add(iid)
        out.append((it, float(score)))
        if len(out) >= max(0, top_k):
            break
    return out

    qtoks = _content_toks(query)  # noqa: F841 — leaf scan kept, never the query door
    # First pass: all query-anchored 512-char leaves. df over those leaves
    # (not the 33 mega-docs). Then drop leaves that lack a rare query token
    # so vault/project decoy windows cannot win.
    raw = []
    for i, it in enumerate(items):
        text = it.get("text") or ""
        iid = it.get("id") or str(i)
        chunks = _query_chunks(text, qtoks) if text else []
        for ci, ch in enumerate(chunks):
            raw.append((it, iid, ci, ch))
    probe = _df_texts([ch for *_, ch in raw], qtoks) if raw else {}
    n_probe = max(1, len(raw))
    rare_q = [t for t in qtoks if probe.get(t, 0) < max(2, n_probe // 2)]
    if not rare_q:
        rare_q = qtoks
    windows = []
    leaf_texts = []
    for it, iid, ci, ch in raw:
        if rare_q and not any(_token_positions(ch, t, limit=1) for t in rare_q):
            continue
        windows.append((it, iid, ci, ch))
        leaf_texts.append(ch)
    vocab = set(qtoks)
    for ch in leaf_texts:
        vocab.update(_content_toks(ch))
        for tok in tokenize(ch):
            if tok.isdigit() and len(tok) >= 3:
                vocab.add(tok)
    n_docs = max(1, len(leaf_texts))
    df = _df_texts(leaf_texts, list(vocab)) if vocab else {}

    q = np.asarray(_record(query, df=df, n_docs=n_docs), dtype=np.float32)
    if float(np.linalg.norm(q)) == 0.0:
        # Fail closed. A 0.0 hit is not retrieval (Fable-5).
        return []

    vecs, labels, owners = [], [], []
    for it, iid, ci, ch in windows:
        v = np.asarray(_record(ch, df=df, n_docs=n_docs), dtype=np.float32)
        if float(np.linalg.norm(v)) == 0.0:
            continue
        vecs.append(v)
        labels.append("%s#%d" % (iid, ci))
        owners.append(it)
    if not vecs:
        return []

    hits = _index_hits(q, vecs, labels, top_k=max(top_k * 4, top_k))
    by_lab = {lab: it for lab, it in zip(labels, owners)}
    seen, out = set(), []
    for lab, score in hits:
        if float(score) <= 0.0:
            continue
        it = by_lab.get(lab)
        if it is None:
            continue
        iid = it.get("id")
        if iid in seen:
            continue
        seen.add(iid)
        out.append((it, float(score)))
        if len(out) >= max(0, top_k):
            break
    return out


def rank_items(query: str, items: list, top_k: int = 8):
    """Unindexed items: phrase bind + Index, not a bag-of-words scan."""
    if not items:
        return []
    filled = []
    for it in items:
        rec = dict(it)
        v = rec.get("vec")
        if v is None or len(v) == 0:  # ndarray-safe, same as rank_stored
            rec["vec"] = item_vector(rec.get("text") or "", rec.get("id") or "")
        filled.append(rec)
    return rank_stored(query, filled, top_k=top_k) or []


# ---------------------------------------------------------------------------
# QUERY NORMALISATION — the instruction words are not the question.
#
# MEASURED, real BM25, 300-chunk corpus with three scattered needles:
#   "List every distinct mention of pump.fun you can find, with context.
#    Be exhaustive."            -> 1/3 needles at top_k=16
#   "pumpfun pump fun bonding curve"  -> 3/3
# BM25 scores a document against EVERY query term, so ten imperative words
# ("list", "every", "find", "context", "exhaustive") spread the score across
# the whole corpus and bury the one term that identifies the target. The more
# carefully a user phrases an exhaustive request, the worse retrieval gets —
# which is the exact production failure: an agent asked for every pump.fun
# mention, got a confident partial answer, and grep found the rest.
#
# THE REWRITE IS GATED, per leCore's expand_query kept negative ("a null
# detects irrelevance, not infidelity -- the primary gate is overlap with the
# ORIGINAL"): we only ever DROP known instruction words, never substitute or
# invent, so the rewrite is a subset of the original by construction and cannot
# drift. If stripping leaves nothing, the original stands.
#
# And it is UNIONed, not swapped: both queries rank, candidates merge. A
# normalisation that helps adds needles; one that hurts cannot subtract them.

_INSTRUCTION_WORDS = frozenset("""
list lists listing every all each any find finding found show shows tell give
gives provide mention mentions mentioned distinct exhaustive exhaustively
comprehensive complete completely please can could would you your me my we our
with without context including include included detail details detailed
explain describe summarise summarize summary about regarding concerning
question answer please thanks thank
what which where when who whom whose why how
is are was were be been being do does did done has have had
the a an of to in on for from by as at and or but if then than that this these
those it its there here also more most some many much very just only
""".split())


def normalize_query(query: str) -> str:
    """Drop instruction boilerplate, keep the content terms. Subset-only.

    Returns "" when nothing distinctive survives — the caller keeps the
    original rather than retrieving on an empty string."""
    raw = query or ""
    toks = [t for t in tokenize(raw)]
    kept = [t for t in toks if t not in _INSTRUCTION_WORDS]

    # COMPOUND VARIANTS. tokenize("pump.fun") -> ["pump", "fun"], so a corpus
    # written "pumpfun" is unreachable from a query written "pump.fun" -- the
    # two forms share no token. MEASURED on the real ranker, 300 chunks with
    # three scattered needles spelling it both ways:
    #   "pump fun"          1/3      "pumpfun"           2/3
    #   "pumpfun pump fun"  3/3
    # So emit the JOINED form alongside the split one for dotted/hyphenated
    # compounds. Additive and drop-only-plus-joins: still a function of what
    # the user wrote, never a substitution.
    for m in re.findall(r"[A-Za-z0-9]+(?:[.\-_][A-Za-z0-9]+)+", raw):
        joined = re.sub(r"[.\-_]", "", m).lower()
        if joined and joined not in kept:
            kept.append(joined)
    # Nothing distinctive left, or we stripped essentially everything the user
    # said: not a rewrite worth trusting.
    if not kept or len(kept) < 1:
        return ""
    return " ".join(kept)
