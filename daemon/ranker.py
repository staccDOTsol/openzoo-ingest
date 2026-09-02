"""DEAD lookalike. Do not import. Query door is rank.py → leCore Index.nearest."""
raise RuntimeError("ranker.py is TF-IDF cosine. Use rank.py (leCore Index.nearest).")

"""Bag-of-words TF·IDF cosine ranker (pure Python). Higher score = better."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_TOKEN = re.compile(r"[a-z0-9]+", re.I)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _tf(tokens: list[str]) -> dict[str, float]:
    if not tokens:
        return {}
    c = Counter(tokens)
    n = float(len(tokens))
    return {t: cnt / n for t, cnt in c.items()}


def _idf(docs_tokens: list[list[str]]) -> dict[str, float]:
    N = len(docs_tokens) or 1
    df: Counter[str] = Counter()
    for toks in docs_tokens:
        df.update(set(toks))
    return {t: math.log(1.0 + N / (1.0 + d)) + 1.0 for t, d in df.items()}


def _dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _norm(a: dict[str, float]) -> float:
    return math.sqrt(sum(v * v for v in a.values())) or 1e-12


def rank_items(
    items: list[dict[str, Any]],
    query: str,
    top_k: int = 8,
) -> list[tuple[dict[str, Any], float]]:
    """Return (item, score) pairs sorted by descending score."""
    if not items:
        return []
    q_toks = tokenize(query)
    if not q_toks:
        return [(it, 0.0) for it in items[:top_k]]

    docs = [tokenize(it.get("text") or "") for it in items]
    idf = _idf(docs + [q_toks])
    q_tf = _tf(q_toks)
    q_vec = {t: q_tf.get(t, 0.0) * idf.get(t, 0.0) for t in q_tf}
    qn = _norm(q_vec)

    scored: list[tuple[dict[str, Any], float]] = []
    for it, toks in zip(items, docs):
        tf = _tf(toks)
        d_vec = {t: tf.get(t, 0.0) * idf.get(t, 0.0) for t in tf}
        score = _dot(q_vec, d_vec) / (qn * _norm(d_vec))
        scored.append((it, float(score)))

    scored.sort(key=lambda x: (-x[1], x[0].get("id") or ""))
    return scored[: max(0, top_k)]


def tokens_est(text: str) -> int:
    """Rough token estimate: chars/4."""
    return max(1, (len(text) + 3) // 4) if text else 0


def truncate_to_budget(text: str, budget: int) -> str:
    if budget is None or budget <= 0:
        return ""
    max_chars = budget * 4
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."
