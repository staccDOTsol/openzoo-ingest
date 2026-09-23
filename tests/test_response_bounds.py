"""HTTP replies are rejected before an oversized body can be buffered."""
from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))

import bounded_response as br  # noqa: E402


class Headers:
    def __init__(self, pairs):
        self._pairs = {}
        for name, value in pairs:
            self._pairs.setdefault(name.lower(), []).append(value)

    def get_all(self, name):
        return list(self._pairs.get(name.lower(), [])) or None


class FakeResp:
    def __init__(self, body, pairs, limit):
        self.headers = Headers(pairs)
        self._body = body
        self._pos = 0
        self.limit = limit
        self.reads = 0

    def read(self, n=None):
        if n is None or n < 0:
            raise AssertionError("unbounded read")
        if self._pos + n > self.limit + 1:
            raise AssertionError("read of %s passes the max+1 ceiling" % n)
        self.reads += 1
        take = min(n, len(self._body) - self._pos)
        chunk = self._body[self._pos : self._pos + take]
        self._pos += len(chunk)
        return chunk


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        mode = self.server.mode
        if mode == "hold":
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(self.server.declared))
            self.end_headers()
            self.server.headers_sent.set()
            self.server.release.wait(5)
            return
        body = self.server.body
        if mode == "chunked":
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(("%x\r\n" % len(body)).encode("ascii"))
            self.wfile.write(body + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
            return
        if mode == "gzip":
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mode == "both":
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.close_connection = True
            return
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        if self.server.declare:
            declared = self.server.declared
            if declared is None:
                declared = len(body)
            self.send_header("Content-Length", str(declared))
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True


def wait_port(port):
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            threading.Event().wait(0.02)
    raise RuntimeError("server did not start")


class Server:
    def __init__(self, mode, body=b"", status=200, declared=None, declare=True):
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.mode = mode
        self.httpd.body = body
        self.httpd.status = status
        self.httpd.declared = declared
        self.httpd.declare = declare
        self.httpd.headers_sent = threading.Event()
        self.httpd.release = threading.Event()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        wait_port(self.port)
        return self

    def __exit__(self, *exc):
        self.httpd.release.set()
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()

    @property
    def port(self):
        return self.httpd.server_address[1]


def call(port, **kwargs):
    opts = dict(
        url="http://127.0.0.1:%d/v1/hrr/bind" % port,
        body={"ping": True},
        headers={"Authorization": "Bearer test"},
        timeout=2,
        shape="context",
        max_bytes=64,
    )
    opts.update(kwargs)
    return br.post_json(**opts)


class BoundedReadTests(unittest.TestCase):
    def test_content_length_over_ceiling_does_not_read(self):
        body = b'{"context_id":"ctx_ok","secret":"LEAKED_BODY"}' + b"x" * 1000
        resp = FakeResp(body, [("Content-Length", "1000000000")], limit=64)
        with self.assertRaises(br.ResponseOverflow):
            br._read_bounded(resp, 64)
        self.assertEqual(resp.reads, 0)

    def test_stream_stops_at_max_plus_one(self):
        resp = FakeResp(b"x" * 500, [], limit=32)
        with self.assertRaises(br.ResponseOverflow):
            br._read_bounded(resp, 32)
        self.assertLessEqual(resp._pos, 33)

    def test_short_content_length_is_not_parsed_as_a_prefix(self):
        raw = b'{"context_id":"ctx_ok"}'
        resp = FakeResp(raw, [("Content-Length", "100")], limit=200)
        with self.assertRaises(br.ResponseError) as caught:
            br._read_bounded(resp, 200)
        self.assertNotIsInstance(caught.exception, br.ResponseOverflow)
        self.assertLessEqual(resp._pos, len(raw))

    def test_multiple_or_invalid_lengths_are_rejected_unread(self):
        for pairs in (
            [("Content-Length", "1"), ("Content-Length", "2")],
            [("Content-Length", "-1")],
            [("Content-Length", "01")],
            [("Content-Length", "10 ")],
            [("Content-Length", "1e2")],
        ):
            resp = FakeResp(b'{"context_id":"ctx_ok"}', pairs, limit=64)
            with self.assertRaises(br.ResponseError):
                br._read_bounded(resp, 64)
            self.assertEqual(resp.reads, 0, pairs)

    def test_chunked_with_content_length_is_rejected_unread(self):
        resp = FakeResp(
            b'{"context_id":"ctx_ok"}',
            [("Content-Length", "22"), ("Transfer-Encoding", "chunked")],
            limit=64,
        )
        with self.assertRaises(br.ResponseError):
            br._read_bounded(resp, 64)
        self.assertEqual(resp.reads, 0)

    def test_exact_length_returns_only_those_bytes(self):
        raw = b'{"context_id":"ctx_ok"}'
        resp = FakeResp(raw + b"EXTRA", [("Content-Length", str(len(raw)))], limit=64)
        # The fake does not clip at Content-Length, so the extra byte is a mismatch.
        with self.assertRaises(br.ResponseError):
            br._read_bounded(resp, 64)
        exact = FakeResp(raw, [("Content-Length", str(len(raw)))], limit=64)
        self.assertEqual(br._read_bounded(exact, 64), raw)


class PostJsonTests(unittest.TestCase):
    def test_success_keeps_only_the_context_id(self):
        raw = b'{"context_id":"ctx_ok","drop":"SECRET_FIELD","items":["nope"]}'
        with Server("plain", raw, declared=len(raw)) as srv:
            got = call(srv.port, max_bytes=br.REMOTE_MAX_BYTES)
        self.assertEqual(got, {"context_id": "ctx_ok"})
        self.assertNotIn("SECRET_FIELD", json.dumps(got))

    def test_non_success_is_unread_and_not_parsed(self):
        raw = b'{"context_id":"LEAKED_SECRET_VALUE"}'
        with Server("hold", raw, status=500, declared=10**9) as srv:
            box = []
            done = threading.Event()

            def run():
                try:
                    box.append(call(srv.port, max_bytes=64))
                except Exception as exc:  # noqa: BLE001 - the assertion is the type
                    box.append(exc)
                finally:
                    done.set()

            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(done.wait(1.5), "client blocked on a non-success body")
        self.assertEqual(len(box), 1)
        self.assertIsInstance(box[0], br.RemoteStatus)
        self.assertEqual(box[0].status, 500)
        self.assertNotIn("LEAKED_SECRET_VALUE", str(box[0]))

    def test_huge_content_length_fails_before_the_body(self):
        with Server("hold", b"x", status=200, declared=10**9) as srv:
            box = []
            done = threading.Event()

            def run():
                try:
                    box.append(call(srv.port, max_bytes=64))
                except Exception as exc:  # noqa: BLE001
                    box.append(exc)
                finally:
                    done.set()

            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(done.wait(1.5), "client buffered a declared-huge body")
        self.assertIsInstance(box[0], br.ResponseOverflow)
        self.assertNotIn("LEAKED", str(box[0]))

    def test_redirect_is_not_followed(self):
        with Server("hold", b"not-json", status=302, declared=10**8) as srv:
            with self.assertRaises(br.RemoteStatus) as caught:
                call(srv.port, timeout=2)
        self.assertEqual(caught.exception.status, 302)

    def test_stream_overflow_is_not_parsed(self):
        raw = b'{"context_id":"ctx_ok"}' + b" "
        with Server("plain", raw, declare=False) as srv:
            with self.assertRaises(br.ResponseOverflow):
                call(srv.port, max_bytes=len(raw) - 1)

    def test_stream_without_length_accepts_a_small_body(self):
        raw = b'{"context_id":"ctx_ok"}'
        with Server("plain", raw, declare=False) as srv:
            self.assertEqual(call(srv.port, max_bytes=64), {"context_id": "ctx_ok"})

    def test_short_declared_length_fails_closed(self):
        raw = b'{"context_id":"ctx_ok"}'
        with Server("plain", raw, declared=len(raw) + 50) as srv:
            with self.assertRaises(br.ResponseError) as caught:
                call(srv.port, max_bytes=br.REMOTE_MAX_BYTES)
        self.assertNotIsInstance(caught.exception, br.ResponseOverflow)
        self.assertNotIn("ctx_ok", str(caught.exception))

    def test_chunked_overflow_and_success(self):
        small = b'{"context_id":"ctx_ok"}'
        with Server("chunked", small) as srv:
            self.assertEqual(call(srv.port, max_bytes=64), {"context_id": "ctx_ok"})
        big = b'{"context_id":"ctx_ok","pad":"' + b"A" * 80 + b'"}'
        with Server("chunked", big) as srv:
            with self.assertRaises(br.ResponseOverflow):
                call(srv.port, max_bytes=32)

    def test_gzip_and_ambiguous_framing_are_rejected(self):
        raw = b'{"context_id":"ctx_ok"}'
        with Server("gzip", raw) as srv:
            with self.assertRaises(br.ResponseError):
                call(srv.port, max_bytes=64)
        with Server("both", raw) as srv:
            with self.assertRaises(br.ResponseError):
                call(srv.port, max_bytes=64)

    def test_vision_drops_extra_fields_and_caps_content(self):
        raw = json.dumps({
            "id": "chatcmpl",
            "choices": [{"message": {"role": "assistant", "content": "a window"}, "extra": "Z" * 50}],
            "usage": {"prompt_tokens": 9},
        }).encode()
        with Server("plain", raw, declared=len(raw)) as srv:
            got = call(srv.port, shape="vision", max_bytes=br.LOCAL_MAX_BYTES)
        self.assertEqual(got, {"choices": [{"message": {"content": "a window"}}]})
        self.assertLessEqual(br.REMOTE_MAX_BYTES, br.LOCAL_MAX_BYTES)
        huge = json.dumps({
            "choices": [{"message": {"content": "y" * (br.MAX_VISION_CHARS + 1)}}],
        }).encode()
        self.assertLess(len(huge), br.REMOTE_MAX_BYTES)
        with Server("plain", huge, declared=len(huge)) as srv:
            with self.assertRaises(br.ResponseOverflow):
                call(srv.port, shape="vision", max_bytes=br.LOCAL_MAX_BYTES)

    def test_recall_caps_items_and_drops_unknown_fields(self):
        raw = json.dumps({
            "object": "hrr.recall",
            "chunks": 99,
            "items": [
                {"id": "a", "text": "alpha", "score": 0.5, "metadata": {"source": "clipboard"}, "vec": [1, 2, 3]},
                {"id": "b", "text": "beta", "score": 0.2, "metadata": {"source": "files"}},
                {"id": "c", "text": "gamma", "score": 0.1, "metadata": {}},
            ],
        }).encode()
        with Server("plain", raw, declared=len(raw)) as srv:
            got = call(srv.port, shape="recall", max_bytes=br.LOCAL_MAX_BYTES, recall_limit=2)
        self.assertEqual(got, {
            "items": [
                {"id": "a", "text": "alpha", "score": 0.5, "metadata": {"source": "clipboard"}},
                {"id": "b", "text": "beta", "score": 0.2, "metadata": {"source": "files"}},
            ],
        })

    def test_hostile_context_id_is_not_retained(self):
        raw = json.dumps({"context_id": "ctx_ok\ninjected"}).encode()
        with Server("plain", raw, declared=len(raw)) as srv:
            with self.assertRaises(br.ResponseOverflow):
                call(srv.port, max_bytes=64)

    def test_nesting_over_the_cap_fails_closed(self):
        raw = b'{"a":' * 9 + b"1" + b"}" * 9
        self.assertLess(len(raw), 64)
        with Server("plain", raw, declared=len(raw)) as srv:
            with self.assertRaises(br.ResponseOverflow):
                call(srv.port, max_bytes=64)

    def test_ceiling_cannot_be_raised_past_the_local_max(self):
        with self.assertRaises(br.ResponseError):
            br.post_json(
                "http://127.0.0.1:9/", {}, {}, 1,
                shape="context", max_bytes=br.LOCAL_MAX_BYTES + 1,
            )

    def test_oversized_metadata_integer_is_not_retained(self):
        with self.assertRaises(br.ResponseOverflow):
            br._retain_recall(
                {"items": [{"text": "hi", "metadata": {"n": 10**1000}}]},
                1,
            )

    def test_client_source_no_longer_buffers_the_whole_reply(self):
        src = (ROOT / "bin" / "openzoo-ingest").read_text()
        self.assertNotIn("r.read().decode()", src)
        self.assertNotIn("json.loads(r.read()", src)
        self.assertIn('shape="vision"', src)
        self.assertIn('shape="context"', src)
        self.assertIn('shape="recall"', src)
        self.assertIn("REMOTE_MAX_BYTES", src)
        self.assertIn("LOCAL_MAX_BYTES", src)
        self.assertIn("RemoteStatus", src)


if __name__ == "__main__":
    unittest.main()
