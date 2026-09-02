"""Import the real leCore catalog — packaged tree first (CAPABILITIES.md)."""
from __future__ import annotations

import os
import sys


def lecore_root() -> str:
    candidates = [
        os.environ.get("LECORE_PATH"),
        "/Users/stacc/core/leCore",
        os.path.expanduser("~/core/leCore"),
        os.path.expanduser("~/leCore"),
        "/app/lecore",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "vendor", "lecore")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "leCore")),
    ]
    for p in candidates:
        if not p:
            continue
        if os.path.isfile(os.path.join(p, "holographic", "caching_and_storage", "holographic_index.py")):
            return p
        if os.path.isfile(os.path.join(p, "holographic_ai.py")):
            return p
    raise RuntimeError(
        "leCore not found. Set LECORE_PATH to the tree that has CAPABILITIES.md. "
        "Do not fall back to a hand-rolled VSA."
    )


def _packaged(root: str) -> bool:
    return os.path.isdir(os.path.join(root, "holographic"))


def import_memory():
    """Memory/search/recall homes from CAPABILITIES.md."""
    root = lecore_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    if _packaged(root):
        from holographic.agents_and_reasoning.holographic_ai import (  # noqa: E402
            Vocabulary,
            bind,
            bundle,
            cosine,
            unbind,
            nearest,
        )
        from holographic.caching_and_storage.holographic_index import Index  # noqa: E402
        if Index is None:
            raise RuntimeError("packaged leCore has no Index — refuse lookalike")
        from holographic.mesh_and_geometry.holographic_planshape import (  # noqa: E402
            ShapeVocab,
            encode_record,
            decode_record,
        )
        from holographic.misc.holographic_superposed import (  # noqa: E402
            pack,
            score_all,
            hierarchical_pack,
            hierarchical_recall,
        )
        from holographic.misc.holographic_determinism import argmax_tiebreak  # noqa: E402

        # Do NOT construct UnifiedMind here. That pulls the 510-faculty
        # surface and OOM'd / 503'd the sidecar mid-bind on 20MB chunks.
        return {
            "root": root,
            "packaged": True,
            "Vocabulary": Vocabulary,
            "bind": bind,
            "bundle": bundle,
            "cosine": cosine,
            "unbind": unbind,
            "nearest": nearest,
            "Index": Index,
            "ShapeVocab": ShapeVocab,
            "encode_record": encode_record,
            "decode_record": decode_record,
            "pack": pack,
            "score_all": score_all,
            "hierarchical_pack": hierarchical_pack,
            "hierarchical_recall": hierarchical_recall,
            "argmax_tiebreak": argmax_tiebreak,
            "mind": None,
        }

    from holographic_ai import Vocabulary, bind, bundle, cosine, unbind, nearest  # noqa: E402
    from holographic_planshape import ShapeVocab, encode_record, decode_record  # noqa: E402
    from holographic_superposed import pack, score_all  # noqa: E402
    from holographic_determinism import argmax_tiebreak  # noqa: E402

    return {
        "root": root,
        "packaged": False,
        "Vocabulary": Vocabulary,
        "bind": bind,
        "bundle": bundle,
        "cosine": cosine,
        "unbind": unbind,
        "nearest": nearest,
        "Index": None,  # flat dump is not CAPABILITIES.md — import_memory refuses below
        "ShapeVocab": ShapeVocab,
        "encode_record": encode_record,
        "decode_record": decode_record,
        "pack": pack,
        "score_all": score_all,
        "hierarchical_pack": None,
        "hierarchical_recall": None,
        "argmax_tiebreak": argmax_tiebreak,
        "mind": None,
    }
