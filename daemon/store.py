"""Persistent ContextStore — sticky agent memory for HRR dogfood."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

try:
    from .ulid import new_ulid
except ImportError:  # script / sys.path mode
    from ulid import new_ulid

CTX_RE = re.compile(r"^ctx_[0-9A-HJKMNP-TV-Z]{26}$")
ITEM_RE = re.compile(r"^m_[0-9A-HJKMNP-TV-Z]{10,26}$")


class StoreError(Exception):
    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


class ContextStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, context_id: str) -> Path:
        safe = "".join(c for c in context_id if c.isalnum() or c in "_-")
        return self.root / f"{safe}.json"

    def _holo_dir(self, context_id: str) -> Path:
        safe = "".join(c for c in context_id if c.isalnum() or c in "_-")
        return self.root / safe

    def state_version(self, context_id: str):
        """Cheap mutation token for the corpus cache: (mtime_ns, size) of
        state.npz. _write rewrites state.npz on EVERY bind/unbind, so any
        mutation changes this. None = no npz store (legacy JSON) — uncacheable."""
        try:
            st = os.stat(self._holo_dir(context_id) / "state.npz")
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _write(self, ctx: dict) -> None:
        """Persist with leCore-shaped compression, not pretty JSON.

        Vectors go in a compressed .npz (float32 — cosine-safe, half the
        JSON-float bloat). Passage bytes are zlib'd once per content-hash
        (dedup). JSON is no longer the store.
        """
        d = self._holo_dir(ctx["context_id"])
        d.mkdir(parents=True, exist_ok=True)
        blobs = d / "blobs"
        blobs.mkdir(exist_ok=True)
        items = ctx.get("items") or []
        ids, hashes, seen, added, paths = [], [], [], [], []
        vecs = []
        for it in items:
            iid = str(it["id"])
            h = str(it.get("hash") or "")
            ids.append(iid)
            hashes.append(h)
            seen.append(int(it.get("seen") or 1))
            added.append(float(it.get("added_at") or 0))
            paths.append(str((it.get("metadata") or {}).get("path") or ""))
            v = it.get("vec")
            vecs.append(np.asarray(v if v else [0.0] * 1024, dtype=np.float32))
            raw = (it.get("text") or "").encode("utf-8")
            bp = blobs / (h or iid)
            if not bp.exists():
                bp.write_bytes(zlib.compress(raw, 9))
        # Atomic swap: savez straight onto state.npz let a concurrent reader
        # stat/load a half-written file, which poisons the cache version token.
        tmp = d / "state.tmp.npz"
        np.savez_compressed(
            tmp,
            ids=np.asarray(ids),
            hashes=np.asarray(hashes),
            seen=np.asarray(seen, dtype=np.int32),
            added=np.asarray(added, dtype=np.float64),
            vecs=np.stack(vecs) if vecs else np.zeros((0, 1024), dtype=np.float32),
            tenant=np.asarray(ctx["tenant_id"]),
            created=np.asarray(float(ctx.get("created_at") or 0.0)),
            disabled=np.asarray(int(bool(ctx.get("disabled")))),
            paths=np.asarray(paths),
        )
        os.replace(tmp, d / "state.npz")
        # drop any leftover pretty-JSON from the old store
        old = self._path(ctx["context_id"])
        if old.exists():
            try:
                old.unlink()
            except OSError:
                pass

    def _quarantine(self, path: Path, reason: str) -> None:
        """Move damaged store file aside so we fail closed (404) instead of 500."""
        try:
            q = path.with_suffix(path.suffix + ".corrupt")
            # unique if prior quarantine exists
            if q.exists():
                q = path.with_suffix(path.suffix + ".corrupt.%d" % int(time.time()))
            os.replace(path, q)
            marker = path.with_suffix(".quarantine.txt")
            marker.write_text("quarantined %s: %s\n" % (path.name, reason))
        except OSError:
            pass

    def _sanitize_items(self, items, keep_textless: bool = False) -> List[dict]:
        """Drop malformed items; damage-tolerant read path.

        keep_textless=True: the caller deliberately skipped text AND vec
        hydration (load_vecs=False fast path) — an empty item there is a
        loading choice, not damage, so it must survive."""
        out = []
        if not isinstance(items, list):
            return out
        for it in items:
            if not isinstance(it, dict):
                continue
            text = it.get("text")
            iid = it.get("id")
            if not iid:
                continue
            has_vec = bool(it.get("vec"))
            if (text is None or str(text) == "") and not has_vec and not keep_textless:
                continue
            kept = {
                "id": str(iid),
                "text": "" if text is None else str(text),
                "metadata": it.get("metadata") if isinstance(it.get("metadata"), dict) else {},
                "added_at": it.get("added_at") or 0,
                "hash": it.get("hash"),
            }
            # vec is the bind-time index. Dropping it here forced every
            # recall back onto the full-corpus scan after a reload.
            if it.get("vec"):
                kept["vec"] = it["vec"]
            if it.get("seen") is not None:
                kept["seen"] = it["seen"]
            if it.get("last_seen") is not None:
                kept["last_seen"] = it["last_seen"]
            out.append(kept)
        return out

    def _load_holo(
        self, context_id: str, load_text: bool = True, load_vecs: bool = True
    ) -> Optional[dict]:
        d = self._holo_dir(context_id)
        state = d / "state.npz"
        if not state.exists():
            return None
        try:
            z = np.load(state, allow_pickle=False)
            ids = [str(x) for x in z["ids"].tolist()]
            hashes = [str(x) for x in z["hashes"].tolist()]
            seen = z["seen"].tolist() if "seen" in z.files else [1] * len(ids)
            added = z["added"].tolist() if "added" in z.files else [0] * len(ids)
            # vec hydration is the hidden cost: .tolist() of every 1024-dim row
            # on paths (peek/text-load) that never read a vec. Skip on demand.
            vecs = z["vecs"] if load_vecs else np.zeros((0, 0), dtype=np.float32)
            paths = [str(x) for x in z["paths"].tolist()] if "paths" in z.files else [""] * len(ids)
            tenant = str(z["tenant"])
            created = float(z["created"]) if "created" in z.files else 0.0
            disabled = bool(int(z["disabled"])) if "disabled" in z.files else False
        except (OSError, ValueError, KeyError, TypeError):
            return None
        blobs = d / "blobs"
        items = []
        for i, iid in enumerate(ids):
            h = hashes[i] if i < len(hashes) else ""
            text = ""
            if load_text:
                bp = blobs / (h or iid)
                if bp.exists():
                    try:
                        text = zlib.decompress(bp.read_bytes()).decode("utf-8")
                    except (OSError, zlib.error, UnicodeDecodeError):
                        text = ""
            rec = {
                "id": iid,
                "text": text,
                "hash": h,
                "seen": int(seen[i]) if i < len(seen) else 1,
                "added_at": float(added[i]) if i < len(added) else 0,
                "metadata": {"path": paths[i]} if i < len(paths) and paths[i] else {},
            }
            if load_vecs and i < len(vecs):
                rec["vec"] = vecs[i].astype(np.float32).tolist()
            items.append(rec)
        return {
            "context_id": context_id,
            "tenant_id": tenant,
            "created_at": created,
            "disabled": disabled,
            "items": self._sanitize_items(
                items, keep_textless=not (load_text or load_vecs)
            ),
        }

    def _load(
        self, context_id: str, load_text: bool = True, load_vecs: bool = True
    ) -> Optional[dict]:
        holo = self._load_holo(context_id, load_text=load_text, load_vecs=load_vecs)
        if holo is not None:
            return holo
        path = self._path(context_id)
        if not path.exists():
            return None
        try:
            raw = path.read_text()
            ctx = json.loads(raw)
        except (OSError, ValueError, TypeError) as e:
            self._quarantine(path, "json:%s" % e)
            return None
        if not isinstance(ctx, dict) or not ctx.get("context_id") or not ctx.get("tenant_id"):
            self._quarantine(path, "schema")
            return None
        ctx["items"] = self._sanitize_items(ctx.get("items"))
        return ctx

    def mint_id(self) -> str:
        return "ctx_" + new_ulid()

    def mint_item_id(self) -> str:
        return "m_" + new_ulid()[:16]

    def get_for_tenant(
        self,
        tenant_id: str,
        context_id: str,
        load_text: bool = True,
        load_vecs: bool = True,
    ) -> dict:
        if not context_id:
            raise StoreError("invalid_request", "context_id required", 400)
        ctx = self._load(context_id, load_text=load_text, load_vecs=load_vecs)
        # Unknown OR wrong tenant → 404 (anti-enumeration)
        if ctx is None or ctx.get("tenant_id") != tenant_id:
            raise StoreError("context_not_found", "unknown context_id", 404)
        if ctx.get("disabled"):
            raise StoreError("context_forbidden", "context disabled", 403)
        return ctx

    def bind(
        self,
        tenant_id: str,
        items: List[dict],
        context_id: Optional[str] = None,
        mode: str = "append",
    ) -> dict:
        if not tenant_id:
            raise StoreError("invalid_request", "tenant_id required", 400)
        if mode not in ("append", "replace"):
            raise StoreError("invalid_request", "mode must be append|replace", 400)
        if not isinstance(items, list) or not items:
            raise StoreError("invalid_request", "items must be a non-empty list", 400)

        with self._lock:
            minted = False
            if context_id:
                ctx = self._load(context_id, load_text=False)
                if ctx is None:
                    # Allow bind-with-id only if format valid and we create under tenant
                    if not CTX_RE.match(context_id):
                        raise StoreError("invalid_request", "invalid context_id format", 400)
                    ctx = {
                        "context_id": context_id,
                        "tenant_id": tenant_id,
                        "created_at": time.time(),
                        "disabled": False,
                        "items": [],
                    }
                    minted = True
                elif ctx.get("tenant_id") != tenant_id:
                    raise StoreError("context_not_found", "unknown context_id", 404)
                elif ctx.get("disabled"):
                    raise StoreError("context_forbidden", "context disabled", 403)
            else:
                context_id = self.mint_id()
                ctx = {
                    "context_id": context_id,
                    "tenant_id": tenant_id,
                    "created_at": time.time(),
                    "disabled": False,
                    "items": [],
                }
                minted = True

            if mode == "replace":
                ctx["items"] = []

            assigned = []
            for raw in items:
                if not isinstance(raw, dict):
                    raise StoreError("invalid_request", "each item must be an object", 400)
                text = raw.get("text")
                if text is None or str(text) == "":
                    raise StoreError("invalid_request", "item.text required", 400)
                iid = raw.get("id") or self.mint_item_id()
                iid = str(iid)
                meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
                # upsert by id
                ctx["items"] = [it for it in ctx["items"] if it.get("id") != iid]
                text_s = str(text)
                h = hashlib.sha256(text_s.encode("utf-8")).hexdigest()[:16]
                # content-hash dedupe: same text under another id → bump seen, keep first id
                existing = next((it for it in ctx["items"] if it.get("hash") == h), None)
                if existing is not None and existing.get("id") != iid:
                    existing["seen"] = int(existing.get("seen") or 1) + 1
                    existing["last_seen"] = time.time()
                    assigned.append(existing["id"])
                    continue
                item = {
                    "id": iid,
                    "text": text_s,
                    "metadata": meta,
                    "added_at": time.time(),
                    "hash": h,
                    "seen": 1,
                }
                # Index at WRITE time. Ranking used to re-tokenise and
                # re-vectorise every item's full text on every query, so a
                # 99M-word context exceeded the gateway's 10s attach timeout
                # and recall failed closed. Paying it once here makes a query
                # a single matmul.
                try:
                    from rank import item_vector

                    item["vec"] = item_vector(text_s, iid)
                except Exception:
                    pass  # unindexed items fall back to the legacy scan
                ctx["items"].append(item)
                assigned.append(iid)

            ctx["updated_at"] = time.time()
            self._write(ctx)
            return {
                "object": "hrr.bind",
                "context_id": ctx["context_id"],
                "bound": len(assigned),
                "item_ids": assigned,
                "minted": minted,
            }

    def unbind(
        self,
        tenant_id: str,
        context_id: str,
        item_ids: Optional[List[str]] = None,
        all_items: bool = False,
    ) -> dict:
        item_ids = item_ids or []
        if all_items and item_ids:
            raise StoreError(
                "invalid_request", "provide item_ids OR all:true, not both", 400
            )
        if not all_items and not item_ids:
            raise StoreError(
                "invalid_request", "provide non-empty item_ids or all:true", 400
            )
        with self._lock:
            ctx = self.get_for_tenant(tenant_id, context_id, load_text=False)
            before = len(ctx["items"])
            if all_items:
                ctx["items"] = []
                unbound = before
            else:
                want = set(item_ids)
                ctx["items"] = [it for it in ctx["items"] if it.get("id") not in want]
                unbound = before - len(ctx["items"])
            ctx["updated_at"] = time.time()
            # Unbind used to leave zlib blobs on disk. That is how the
            # 100M wiki run was recoverable after all:true, and it is
            # also a leak. Drop any blob no remaining item still hashes.
            self._drop_orphan_blobs(context_id, ctx["items"])
            self._write(ctx)
            return {
                "object": "hrr.unbind",
                "context_id": context_id,
                "unbound": unbound,
            }

    def _drop_orphan_blobs(self, context_id: str, items: List[dict]) -> None:
        blobs = self._holo_dir(context_id) / "blobs"
        if not blobs.is_dir():
            return
        keep = {str(it.get("hash") or "") for it in items}
        keep.discard("")
        try:
            names = os.listdir(blobs)
        except OSError:
            return
        for name in names:
            if name in keep:
                continue
            bp = blobs / name
            try:
                if bp.is_file():
                    bp.unlink()
            except OSError:
                pass

    def list_items(self, tenant_id: str, context_id: str, load_text: bool = True) -> List[dict]:
        with self._lock:
            ctx = self.get_for_tenant(tenant_id, context_id, load_text=load_text)
            return list(ctx.get("items") or [])

    def peek_prefixes(self, tenant_id: str, context_id: str, item_ids: List[str], n: int = 240) -> dict:
        """First n decoded chars. Incremental zlib — do not inflate 8MB HTML."""
        want = {str(i) for i in item_ids if i}
        out: dict = {}
        if not want:
            return out
        with self._lock:
            ctx = self.get_for_tenant(
                tenant_id, context_id, load_text=False, load_vecs=False
            )
            blobs = self._holo_dir(context_id) / "blobs"
            for it in ctx.get("items") or []:
                iid = str(it.get("id") or "")
                if iid not in want:
                    continue
                h = str(it.get("hash") or "")
                bp = blobs / (h or iid)
                if not bp.exists():
                    out[iid] = ""
                    continue
                try:
                    dec = zlib.decompressobj()
                    buf = b""
                    with bp.open("rb") as f:
                        while len(buf) < n * 4:
                            chunk = f.read(4096)
                            if not chunk:
                                break
                            buf += dec.decompress(chunk)
                            if dec.eof:
                                break
                    out[iid] = buf[: n * 4].decode("utf-8", "replace")[:n]
                except (OSError, zlib.error, UnicodeDecodeError):
                    out[iid] = ""
        return out

    def load_item_texts(self, tenant_id: str, context_id: str, item_ids: List[str]) -> dict:
        """Decompress only the named items. Rank must not load the whole store."""
        want = {str(i) for i in item_ids if i}
        out: dict = {}
        if not want:
            return out
        with self._lock:
            ctx = self.get_for_tenant(
                tenant_id, context_id, load_text=False, load_vecs=False
            )
            blobs = self._holo_dir(context_id) / "blobs"
            for it in ctx.get("items") or []:
                iid = str(it.get("id") or "")
                if iid not in want:
                    continue
                text = it.get("text") or ""
                if not text:
                    h = str(it.get("hash") or "")
                    bp = blobs / (h or iid)
                    if bp.exists():
                        try:
                            text = zlib.decompress(bp.read_bytes()).decode("utf-8")
                        except (OSError, zlib.error, UnicodeDecodeError):
                            text = ""
                out[iid] = text
        return out
