"""Optional second-stage semantic encoder over the BM25 pipeline — env-gated.

WHAT THIS IS: a REAL encoder (the Unicron-assimilated / requantized bge-base
artifact, HF `staccs/lecore-bge-assimilated`, or any sentence-transformers
directory) used in one of two gated roles on the recall path:

  SEMANTIC_STAGE=off     (default) — module stays cold, ZERO behavior change.
  SEMANTIC_STAGE=rerank  — re-order BM25's top-k candidates by encoder cosine.
                           Candidate SET is unchanged; only the order moves.
  SEMANTIC_STAGE=union   — a parallel semantic recall lane over the corpus;
                           its top-k candidate ids are UNIONed with BM25's
                           before the budget cut. This is the J=0 fix: at zero
                           stem overlap BM25 returns nothing rankable, so the
                           union lane is the only source of candidates.

WHAT THIS IS NOT: the dead dense-VSA encoder. That path MEASURED SciFact
nDCG@10 0.4160 vs 0.6705 for BM25 and score-FUSING it in degraded
monotonically at every weight (wiki: 0.05→0.6596, 0.15→0.6307, 0.30→0.6057).
Two consequences baked in here:
  1. No hand-rolled vectors — this loads trained transformer weights.
  2. No score fusion. rerank/union order purely by encoder cosine; BM25's
     scores are never mixed into the same scale. If a fused ordering is ever
     wanted it must be measured first, not assumed.

ARTIFACT FORMAT (per bge_assim/remote_requant.py): a sentence-transformers
directory whose model.safetensors holds dequantized fp32 weights snapped to
the quantized grid — loads exactly where bge-base loads. An ONNX export
(model.onnx in the directory) is also accepted, via onnxruntime + CLS pooling.

Import cost: this module imports nothing heavy at module import. torch /
sentence-transformers load lazily on first use, and only when
SEMANTIC_STAGE != off. When the stage is ON and the model cannot load, calls
RAISE — a silent fallback to BM25 would be measured as "no change" and poison
the before/after.

Env knobs:
  SEMANTIC_STAGE        off | rerank | union          (default off)
  SEMANTIC_MODEL        HF repo id or local dir       (default staccs/lecore-bge-assimilated)
  SEMANTIC_DEVICE       torch device                  (default cpu)
  SEMANTIC_THREADS      torch.set_num_threads(N)      (default: leave torch's default)
  SEMANTIC_TOPK         union-lane candidate count    (default 64)
  SEMANTIC_BATCH        encode batch size             (default 64)
  SEMANTIC_CACHE_MAX    global text-hash emb cache    (default 250000 entries)
  SEMANTIC_UNION_MAX_ITEMS  union corpus cap          (default 60000; above it,
                        union degrades to rerank — hydrating every text on a
                        fat legacy bind is exactly what 502'd recall at ~40M
                        words, and this lane must not reintroduce it)
  SEMANTIC_QUERY_PREFIX bge query prefix override     (default official bge prefix)
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_MODES = ("off", "rerank", "union")


def mode() -> str:
    """Read the gate per-call (cheap; lets a restart-per-mode harness and a
    long-lived process both see the truth)."""
    m = (os.environ.get("SEMANTIC_STAGE") or "off").strip().lower()
    return m if m in _MODES else "off"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# encoders
# ---------------------------------------------------------------------------


class _StEncoder:
    """sentence-transformers path — the artifact's native format."""

    def __init__(self, model_path: str, device: str):
        import torch  # lazy — never at module import
        from sentence_transformers import SentenceTransformer

        threads = _env_int("SEMANTIC_THREADS", 0)
        if threads > 0:
            torch.set_num_threads(threads)
        self._m = SentenceTransformer(model_path, device=device)
        self._m.eval()
        self._batch = _env_int("SEMANTIC_BATCH", 64)

    def encode(self, texts: List[str]) -> np.ndarray:
        embs = self._m.encode(
            texts,
            batch_size=self._batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(embs, dtype=np.float32)


class _OnnxEncoder:
    """ONNX fallback if that is the shape the assimilation agent ships.

    bge-base pools the CLS token (its 1_Pooling config: cls), then L2-norm.
    """

    def __init__(self, model_dir: str, onnx_file: str):
        try:
            import onnxruntime as ort
            from transformers import AutoTokenizer
        except ImportError as e:  # actionable, not a silent downgrade
            raise RuntimeError(
                "SEMANTIC_MODEL points at an ONNX artifact but onnxruntime/"
                "transformers is not installed: %s" % e
            )
        so = ort.SessionOptions()
        threads = _env_int("SEMANTIC_THREADS", 0)
        if threads > 0:
            so.intra_op_num_threads = threads
        self._sess = ort.InferenceSession(
            onnx_file, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._tok = AutoTokenizer.from_pretrained(model_dir)
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self._batch = _env_int("SEMANTIC_BATCH", 64)

    def encode(self, texts: List[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i : i + self._batch]
            enc = self._tok(
                batch, padding=True, truncation=True, max_length=512, return_tensors="np"
            )
            feed = {k: v for k, v in enc.items() if k in self._inputs}
            hidden = self._sess.run(None, feed)[0]  # (B, T, H)
            cls = np.asarray(hidden[:, 0, :], dtype=np.float32)
            n = np.linalg.norm(cls, axis=1, keepdims=True)
            n[n == 0] = 1.0
            out.append(cls / n)
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


# ---------------------------------------------------------------------------
# the stage
# ---------------------------------------------------------------------------


class SemanticStage:
    def __init__(self, model_path: Optional[str] = None, device: Optional[str] = None):
        self.model_path = model_path or os.environ.get(
            "SEMANTIC_MODEL", "staccs/lecore-bge-assimilated"
        )
        self.device = device or os.environ.get("SEMANTIC_DEVICE", "cpu")
        self.q_prefix = os.environ.get("SEMANTIC_QUERY_PREFIX", _BGE_QUERY_PREFIX)
        self._enc = self._load(self.model_path, self.device)
        self._lock = threading.Lock()
        # global text-hash → embedding row. Version-agnostic on purpose: an
        # append-mode bind bumps the store version but leaves most chunk texts
        # identical, and re-embedding an unchanged 600-char chunk is pure waste.
        self._emb: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._emb_max = _env_int("SEMANTIC_CACHE_MAX", 250_000)
        self._qcache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        # per-context corpus matrix, keyed by (tenant, cid, store-version) —
        # any bind/unbind changes the version and invalidates naturally.
        self._ctx: "OrderedDict[tuple, Tuple[List[str], np.ndarray]]" = OrderedDict()
        self._ctx_max = 8

    @staticmethod
    def _load(model_path: str, device: str):
        if os.path.isdir(model_path):
            for cand in ("model.onnx", os.path.join("onnx", "model.onnx")):
                p = os.path.join(model_path, cand)
                if os.path.isfile(p):
                    return _OnnxEncoder(model_path, p)
            onnx = [f for f in os.listdir(model_path) if f.endswith(".onnx")]
            if onnx:
                return _OnnxEncoder(model_path, os.path.join(model_path, onnx[0]))
        return _StEncoder(model_path, device)

    # -- embedding ----------------------------------------------------------

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """Hash-cached document embeddings, one row per input text."""
        keys = [hashlib.sha256((t or "").encode("utf-8")).hexdigest() for t in texts]
        with self._lock:
            missing = [
                (k, t) for k, t in dict(zip(keys, texts)).items() if k not in self._emb
            ]
        if missing:
            rows = self._enc.encode([t for _k, t in missing])
            with self._lock:
                for (k, _t), r in zip(missing, rows):
                    self._emb[k] = np.asarray(r, dtype=np.float32)
                while len(self._emb) > self._emb_max:
                    self._emb.popitem(last=False)
        with self._lock:
            return np.vstack([self._emb[k] for k in keys])

    def embed_query(self, query: str) -> np.ndarray:
        """Query embedding with the official bge retrieval prefix (bare
        queries are a known handicap — a fake win for the lexical arm)."""
        with self._lock:
            q = self._qcache.get(query)
            if q is not None:
                return q
        q = self._enc.encode([self.q_prefix + query])[0].astype(np.float32)
        with self._lock:
            self._qcache[query] = q
            while len(self._qcache) > 4096:
                self._qcache.popitem(last=False)
        return q

    @staticmethod
    def cosine(q: np.ndarray, M: np.ndarray) -> np.ndarray:
        """Rows of M and q are already unit-norm — cosine is the matmul."""
        return M @ q

    # -- corpus matrix (union lane) -----------------------------------------

    def corpus_matrix(
        self, cache_key: tuple, ids: List[str], texts: Dict[str, str]
    ) -> Tuple[List[str], np.ndarray]:
        with self._lock:
            hit = self._ctx.get(cache_key)
            if hit is not None:
                self._ctx.move_to_end(cache_key)
                return hit
        keep = [i for i in ids if texts.get(i)]
        M = self.embed_texts([texts[i] for i in keep]) if keep else np.zeros(
            (0, 0), dtype=np.float32
        )
        with self._lock:
            self._ctx[cache_key] = (keep, M)
            while len(self._ctx) > self._ctx_max:
                self._ctx.popitem(last=False)
        return keep, M


_STAGE: Optional[SemanticStage] = None
_STAGE_LOCK = threading.Lock()


def get_stage() -> SemanticStage:
    """Lazy singleton. RAISES when the model cannot load — a stage that is ON
    must never silently degrade to BM25-only (that reads as 'no change')."""
    global _STAGE
    if _STAGE is None:
        with _STAGE_LOCK:
            if _STAGE is None:
                _STAGE = SemanticStage()
    return _STAGE


# ---------------------------------------------------------------------------
# integration point (called from server.handle_recall when mode() != off)
# ---------------------------------------------------------------------------


def apply_stage(
    query: str,
    ranked: List[tuple],
    items: List[dict],
    texts_of: Callable[[List[str]], Dict[str, str]],
    cache_key: tuple,
    top_k: int,
) -> List[tuple]:
    """Second stage over the first-stage candidates.

    ranked   — [(item, score)] from BM25 (or the vec fallback), first stage.
    items    — the full corpus item list (ids at minimum), for the union lane.
    texts_of — hydrator: ids -> {id: text}; caller routes it through the
               per-context cache when one exists so this adds no reload.
    Returns [(item, encoder_cosine)] — scores are encoder cosines, comparable
    within the list; never mixed with BM25's scale (no fusion, by measurement).
    """
    m = mode()
    if m == "off" or not query:
        return ranked
    st = get_stage()
    id2item = {str(it.get("id") or ""): it for it in items}
    q = st.embed_query(query)

    union_cap = _env_int("SEMANTIC_UNION_MAX_ITEMS", 60_000)
    do_union = m == "union" and 0 < len(items) <= union_cap

    if do_union:
        all_ids = [str(it.get("id") or "") for it in items if it.get("id")]
        texts = texts_of(all_ids)
        keep, M = st.corpus_matrix(cache_key, all_ids, texts)
        if len(keep):
            scores = st.cosine(q, M)
            k = min(_env_int("SEMANTIC_TOPK", 64), len(keep))
            top = np.argsort(-scores, kind="stable")[:k]
            sem = {keep[int(i)]: float(scores[int(i)]) for i in top}
            # UNION: BM25's candidate ids join the semantic lane's; every
            # candidate is then ordered by encoder cosine. BM25-only ids that
            # the lane did not score get their cosine computed from the matrix.
            pos = {iid: j for j, iid in enumerate(keep)}
            out_scores = dict(sem)
            for it, _s in ranked:
                iid = str(it.get("id") or "")
                if iid in out_scores:
                    continue
                j = pos.get(iid)
                if j is not None:
                    out_scores[iid] = float(scores[j])
            ordered = sorted(out_scores.items(), key=lambda kv: -kv[1])
            return [
                (id2item[iid], sc)
                for iid, sc in ordered[: max(1, top_k)]
                if iid in id2item
            ]
        # empty corpus text — nothing to add; fall through to rerank of ranked

    # rerank (and union's fat-corpus degradation): re-order the FIRST STAGE's
    # candidate set only. Never widens, never narrows (except the optional
    # depth cap below, which trims the tail BEFORE reordering).
    if not ranked:
        return ranked
    # SEMANTIC_RERANK_MAX (default 0 = whole first-stage list): recall's
    # over-ask hands rerank up to top_k*8 candidates; embedding all of them
    # MEASURED ~2.2 s/query on this Mac's CPU with bge-base. Capping trades
    # recovery depth for latency — a cap of N reranks only BM25's top N.
    cap = _env_int("SEMANTIC_RERANK_MAX", 0)
    if cap > 0:
        ranked = ranked[:cap]
    cand_ids = [str(it.get("id") or "") for it, _s in ranked if it.get("id")]
    texts = texts_of(cand_ids)
    keep = [i for i in cand_ids if texts.get(i)]
    if not keep:
        return ranked
    M = st.embed_texts([texts[i] for i in keep])
    scores = st.cosine(q, M)
    order = np.argsort(-scores, kind="stable")
    return [(id2item[keep[int(i)]], float(scores[int(i)])) for i in order]
