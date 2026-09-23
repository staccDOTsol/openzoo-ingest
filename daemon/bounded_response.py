"""Size-capped JSON replies for the ingest client.

A timeout limits how long a peer may stall. It does not limit how many
bytes that peer may send. This module rejects non-success responses,
refuses a Content-Length above the ceiling before any body byte is
buffered, and otherwise streams at most ceiling+1 bytes so an overflow
is detected and the rest of the body is never read. Parsed documents are
reduced to the fields the caller retains. Overflow fails closed: nothing
is parsed and nothing is kept.
"""
from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlsplit

# Remote brain and vision replies are a context id or a short description.
REMOTE_MAX_BYTES = 64 * 1024
# Local recall returns at most 128 chunks. Ingest chunks are ~700 chars;
# 1 MiB covers that and stays under the daemon's 8 MiB request cap.
LOCAL_MAX_BYTES = 1024 * 1024

MAX_CONTEXT_ID_LEN = 128
MAX_VISION_CHARS = 8_000
MAX_RECALL_ITEMS = 128
MAX_RECALL_TEXT = 32_000
MAX_META_KEYS = 16
MAX_META_KEY = 64
MAX_META_STR = 2048
MAX_JSON_DEPTH = 8
_READ_CHUNK = 8192

_CONTEXT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTENT_LENGTH = re.compile(r"^(0|[1-9][0-9]*)$")
_SHAPES = frozenset({"context", "vision", "recall"})


class ResponseError(Exception):
    """The reply was rejected. The message never includes the body."""


class RemoteStatus(ResponseError):
    """The peer answered with a non-success status. The body was not read."""

    def __init__(self, status: int):
        super().__init__("non-success HTTP %s" % status)
        self.status = status


class ResponseOverflow(ResponseError):
    """The reply exceeded a byte or field ceiling. Nothing was retained."""


def post_json(
    url: str,
    body,
    headers,
    timeout,
    *,
    shape: str,
    max_bytes: int,
    recall_limit: int | None = None,
) -> dict:
    if shape not in _SHAPES:
        raise ResponseError("rejected response shape")
    max_bytes = _ceiling(max_bytes, shape)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ResponseError("rejected timeout")
    scheme, host, port, path = _destination(url)
    payload = json.dumps(body).encode("utf-8")
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    try:
        try:
            conn.request("POST", path, body=payload, headers=_request_headers(headers))
            resp = conn.getresponse()
        except ResponseError:
            raise
        except (http.client.HTTPException, OSError, TimeoutError, ValueError):
            raise ResponseError("response rejected") from None
        try:
            if resp.status < 200 or resp.status > 299:
                raise RemoteStatus(resp.status)
            raw = _read_bounded(resp, max_bytes)
        finally:
            resp.close()
    finally:
        conn.close()
    data = _parse(raw)
    del raw
    if shape == "context":
        return _retain_context(data)
    if shape == "vision":
        return _retain_vision(data)
    return _retain_recall(data, recall_limit)


def _ceiling(max_bytes: int, shape: str) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ResponseError("rejected ceiling")
    if max_bytes < 1 or max_bytes > LOCAL_MAX_BYTES:
        raise ResponseError("rejected ceiling")
    # Vision replies are short text. Never buffer an echoed image.
    if shape == "vision":
        return min(max_bytes, REMOTE_MAX_BYTES)
    return max_bytes


def _destination(url: str):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ResponseError("rejected URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise ResponseError("rejected URL")
    host = parts.hostname
    if not host:
        raise ResponseError("rejected URL")
    try:
        port = parts.port
    except ValueError:
        raise ResponseError("rejected URL") from None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if port < 1 or port > 65535:
        raise ResponseError("rejected URL")
    path = parts.path or "/"
    if not path.startswith("/"):
        raise ResponseError("rejected URL")
    if parts.query:
        path = path + "?" + parts.query
    return parts.scheme, host, port, path


def _request_headers(headers) -> dict:
    out = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if not headers:
        return out
    if not isinstance(headers, dict):
        raise ResponseError("rejected request header")
    banned = {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "content-type",
        "host",
        "transfer-encoding",
    }
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key:
            raise ResponseError("rejected request header")
        if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
            raise ResponseError("rejected request header")
        if key.lower() in banned:
            continue
        out[key] = value
    return out


def _header_values(headers, name: str) -> list:
    if headers is None or not hasattr(headers, "get_all"):
        raise ResponseError("rejected response headers")
    values = headers.get_all(name)
    if not values:
        return []
    return [str(v) for v in values]


def _tokens(headers, name: str) -> list:
    tokens = []
    for value in _header_values(headers, name):
        for part in value.split(","):
            token = part.strip().lower()
            if token:
                tokens.append(token)
    return tokens


def _content_length(resp, limit: int):
    values = _header_values(resp.headers, "Content-Length")
    if not values:
        return None
    raw = values[0]
    if len(values) != 1 or _CONTENT_LENGTH.fullmatch(raw) is None:
        raise ResponseError("rejected Content-Length")
    declared = int(raw)
    if declared > limit:
        raise ResponseOverflow("Content-Length exceeds ceiling")
    return declared


def _reject_encodings(resp, declared) -> None:
    if _tokens(resp.headers, "Content-Encoding") not in ([], ["identity"]):
        raise ResponseError("rejected content encoding")
    transfer = _tokens(resp.headers, "Transfer-Encoding")
    if not transfer:
        return
    # Chunked framing plus Content-Length is ambiguous. Fail closed.
    if transfer != ["chunked"] or declared is not None:
        raise ResponseError("rejected transfer encoding")


def _read_bounded(resp, limit: int) -> bytes:
    """Buffer at most limit+1 bytes. The extra byte means overflow.

    A Content-Length above `limit` raises before the body is read.
    """
    declared = _content_length(resp, limit)
    _reject_encodings(resp, declared)
    buf = bytearray()
    ceiling = limit + 1
    try:
        while len(buf) < ceiling:
            want = min(_READ_CHUNK, ceiling - len(buf))
            try:
                chunk = resp.read(want)
            except http.client.IncompleteRead:
                raise ResponseError("truncated response") from None
            if not isinstance(chunk, (bytes, bytearray)):
                raise ResponseError("rejected response")
            if len(chunk) > want:
                raise ResponseOverflow("response exceeds byte ceiling")
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > limit:
                raise ResponseOverflow("response exceeds byte ceiling")
        if declared is not None and len(buf) != declared:
            raise ResponseError("response does not match Content-Length")
        return bytes(buf)
    finally:
        buf.clear()


def _parse(raw: bytes) -> dict:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ResponseError("rejected response body")
    _check_depth(raw, MAX_JSON_DEPTH)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ResponseError("rejected response body") from None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, OverflowError, ValueError, RecursionError):
        raise ResponseError("rejected response body") from None
    if not isinstance(data, dict):
        raise ResponseError("rejected response body")
    return data


def _check_depth(raw: bytes, max_depth: int) -> None:
    depth = 0
    in_str = False
    esc = False
    for byte in raw:
        if in_str:
            if esc:
                esc = False
            elif byte == 0x5C:
                esc = True
            elif byte == 0x22:
                in_str = False
            continue
        if byte == 0x22:
            in_str = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > max_depth:
                raise ResponseOverflow("response nesting exceeds cap")
        elif byte in (0x7D, 0x5D) and depth:
            depth -= 1


def _retain_context(data: dict) -> dict:
    cid = data.get("context_id")
    if cid is None:
        return {"context_id": None}
    if (
        not isinstance(cid, str)
        or len(cid) > MAX_CONTEXT_ID_LEN
        or _CONTEXT_ID.fullmatch(cid) is None
    ):
        raise ResponseOverflow("context_id exceeds cap")
    return {"context_id": cid}


def _retain_vision(data: dict) -> dict:
    choices = data.get("choices")
    content = ""
    if choices is not None:
        if not isinstance(choices, list):
            raise ResponseError("vision response rejected")
        if len(choices) > 8:
            raise ResponseOverflow("vision response exceeds cap")
        if choices:
            first = choices[0]
            if not isinstance(first, dict):
                raise ResponseError("vision response rejected")
            message = first.get("message")
            if message is None:
                raw = ""
            elif not isinstance(message, dict):
                raise ResponseError("vision response rejected")
            else:
                raw = message.get("content")
                if raw is None:
                    raw = ""
            if not isinstance(raw, str):
                raise ResponseError("vision response rejected")
            if len(raw) > MAX_VISION_CHARS:
                raise ResponseOverflow("vision content exceeds cap")
            content = raw
    return {"choices": [{"message": {"content": content}}]}


def _retain_recall(data: dict, recall_limit: int | None) -> dict:
    if recall_limit is None:
        limit = MAX_RECALL_ITEMS
    elif isinstance(recall_limit, bool) or not isinstance(recall_limit, int):
        raise ResponseError("recall cap rejected")
    else:
        limit = recall_limit
    if limit < 1:
        limit = 1
    if limit > MAX_RECALL_ITEMS:
        limit = MAX_RECALL_ITEMS
    items = data.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ResponseError("recall response rejected")
    if len(items) > MAX_RECALL_ITEMS:
        raise ResponseOverflow("recall items exceed cap")
    kept = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            raise ResponseError("recall response rejected")
        text = item.get("text")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ResponseError("recall response rejected")
        if len(text) > MAX_RECALL_TEXT:
            raise ResponseOverflow("recall text exceeds cap")
        item_id = item.get("id")
        if item_id is not None and (not isinstance(item_id, str) or len(item_id) > 80):
            raise ResponseOverflow("recall id exceeds cap")
        score = item.get("score", 0)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            score = 0.0
        else:
            try:
                score = float(score)
            except (OverflowError, ValueError):
                raise ResponseOverflow("recall score exceeds cap") from None
            if score != score or score == float("inf") or score == float("-inf"):
                score = 0.0
        meta_in = item.get("metadata")
        if meta_in is None:
            meta_in = {}
        if not isinstance(meta_in, dict):
            raise ResponseError("recall response rejected")
        if len(meta_in) > MAX_META_KEYS:
            raise ResponseOverflow("recall metadata exceeds cap")
        meta = {}
        for key, value in meta_in.items():
            if not isinstance(key, str) or not key or len(key) > MAX_META_KEY:
                raise ResponseOverflow("recall metadata exceeds cap")
            if isinstance(value, str):
                if len(value) > MAX_META_STR:
                    raise ResponseOverflow("recall metadata exceeds cap")
                meta[key] = value
            elif value is None or isinstance(value, bool):
                meta[key] = value
            elif isinstance(value, int):
                if value.bit_length() > 64:
                    raise ResponseOverflow("recall metadata exceeds cap")
                meta[key] = value
            elif isinstance(value, float):
                if value != value or value == float("inf") or value == float("-inf"):
                    raise ResponseOverflow("recall metadata exceeds cap")
                meta[key] = value
            else:
                raise ResponseError("recall response rejected")
        kept.append({"id": item_id, "text": text, "score": score, "metadata": meta})
    return {"items": kept}
