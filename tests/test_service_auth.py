"""Security properties for the local memory daemon's authenticator."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))

import service_auth  # noqa: E402


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.token_path = self.home / "service-token"
        os.environ["HRR_TOKEN_FILE"] = str(self.token_path)
        os.environ.pop("HRR_SERVICE_TOKEN", None)
        os.environ.pop("OPENZOO_LECORE_TOKEN", None)

    def test_first_run_writes_user_only_secret(self):
        token = service_auth.load_service_token()
        self.assertGreaterEqual(len(token), service_auth.MIN_TOKEN_LEN)
        self.assertNotEqual(token, service_auth.PUBLIC_DEFAULT_TOKEN)
        mode = self.token_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.token_path.read_text(), token)
        again = service_auth.load_service_token()
        self.assertEqual(again, token)

    def test_public_default_in_env_does_not_grant_access(self):
        os.environ["HRR_SERVICE_TOKEN"] = service_auth.PUBLIC_DEFAULT_TOKEN
        token = service_auth.load_service_token()
        self.assertNotEqual(token, service_auth.PUBLIC_DEFAULT_TOKEN)
        self.assertTrue(self.token_path.is_file())

    def test_public_default_file_is_replaced(self):
        self.token_path.write_text(service_auth.PUBLIC_DEFAULT_TOKEN)
        os.chmod(self.token_path, 0o600)
        token = service_auth.load_service_token()
        self.assertNotEqual(token, service_auth.PUBLIC_DEFAULT_TOKEN)
        self.assertGreaterEqual(len(token), service_auth.MIN_TOKEN_LEN)
        self.assertEqual(self.token_path.read_text(), token)

    def test_client_does_not_invent_a_token(self):
        with self.assertRaises(service_auth.AuthError):
            service_auth.load_service_token(env_var="OPENZOO_LECORE_TOKEN", generate=False)

    def test_client_rejects_public_file_without_rewriting_it(self):
        self.token_path.write_text(service_auth.PUBLIC_DEFAULT_TOKEN)
        os.chmod(self.token_path, 0o600)
        with self.assertRaises(service_auth.AuthError):
            service_auth.load_service_token(generate=False)
        self.assertEqual(self.token_path.read_text(), service_auth.PUBLIC_DEFAULT_TOKEN)

    def test_loose_mode_is_tightened(self):
        strong = "a" * 40
        self.token_path.write_text(strong)
        os.chmod(self.token_path, 0o644)
        got = service_auth.load_service_token(generate=False)
        self.assertEqual(got, strong)
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)

    def test_symlink_token_file_is_refused(self):
        real = self.home / "elsewhere"
        real.write_text("b" * 40)
        self.token_path.symlink_to(real)
        with self.assertRaises(service_auth.AuthError):
            service_auth.load_service_token()

    def test_world_writable_directory_is_refused(self):
        self.token_path.write_text("c" * 40)
        os.chmod(self.token_path, 0o600)
        os.chmod(self.home, 0o777)
        with self.assertRaises(service_auth.AuthError):
            service_auth.load_service_token(generate=False)

    def test_short_env_token_is_refused(self):
        os.environ["HRR_SERVICE_TOKEN"] = "short-but-not-the-default"
        with self.assertRaises(service_auth.AuthError):
            service_auth.load_service_token()

    def test_strong_env_token_is_honored(self):
        os.environ["HRR_SERVICE_TOKEN"] = "e" * 48
        self.assertEqual(service_auth.load_service_token(), "e" * 48)
        self.assertFalse(self.token_path.exists())


class HeaderTests(unittest.TestCase):
    def test_missing_and_wrong_token_are_rejected(self):
        secret = "s" * 40
        self.assertFalse(service_auth.auth_header_ok(secret, {}))
        self.assertFalse(service_auth.auth_header_ok(secret, {"Authorization": "Bearer no"}))
        self.assertFalse(service_auth.auth_header_ok(secret, {"X-HRR-Service-Token": "no"}))

    def test_bearer_and_header_match(self):
        secret = "s" * 40
        self.assertTrue(service_auth.auth_header_ok(secret, {"Authorization": "Bearer " + secret}))
        self.assertTrue(service_auth.auth_header_ok(secret, {"X-HRR-Service-Token": secret}))

    def test_public_default_never_authenticates(self):
        presented = {"Authorization": "Bearer " + service_auth.PUBLIC_DEFAULT_TOKEN}
        self.assertFalse(service_auth.auth_header_ok(service_auth.PUBLIC_DEFAULT_TOKEN, presented))
        self.assertFalse(service_auth.auth_header_ok("", presented))
        self.assertFalse(service_auth.auth_header_ok("short", presented))


class BodyLimitTests(unittest.TestCase):
    def test_default_is_sane_and_override_cannot_reach_32gib(self):
        self.assertEqual(service_auth.resolve_max_body(""), service_auth.DEFAULT_MAX_BODY)
        self.assertLessEqual(service_auth.DEFAULT_MAX_BODY, 8 * 1024 * 1024)
        self.assertEqual(service_auth.resolve_max_body("34359738368"), service_auth.HARD_MAX_BODY)
        self.assertLessEqual(service_auth.HARD_MAX_BODY, 32 * 1024 * 1024)
        self.assertEqual(service_auth.resolve_max_body("0"), service_auth.DEFAULT_MAX_BODY)
        self.assertEqual(service_auth.resolve_max_body("nope"), service_auth.DEFAULT_MAX_BODY)
        self.assertEqual(service_auth.resolve_max_body("4096"), 4096)

    def test_negative_and_huge_lengths_are_rejected(self):
        cap = service_auth.DEFAULT_MAX_BODY
        self.assertEqual(service_auth.check_content_length(None, cap), (0, None))
        self.assertEqual(service_auth.check_content_length("", cap), (0, None))
        self.assertEqual(service_auth.check_content_length("-1", cap), (0, "too_large"))
        self.assertEqual(service_auth.check_content_length("34359738368", cap)[1], "too_large")
        self.assertEqual(service_auth.check_content_length("abc", cap)[1], "bad")
        self.assertEqual(service_auth.check_content_length("12", cap), (12, None))


class SourceRegressionTests(unittest.TestCase):
    def test_daemon_and_client_do_not_ship_the_public_token_as_a_default(self):
        server = (ROOT / "daemon" / "server.py").read_text()
        client = (ROOT / "bin" / "openzoo-ingest").read_text()
        self.assertNotIn("hrr-lab-token", server)
        self.assertNotIn("34_359_738_368", server)
        self.assertIn("load_service_token", server)
        self.assertIn("resolve_max_body", server)
        self.assertIn("check_content_length", server)
        self.assertNotIn('OPENZOO_LECORE_TOKEN", "hrr-lab-token"', client)
        self.assertNotIn("hrr-lab-token", client)
        self.assertIn("load_service_token", client)

    def test_token_file_mode_constant(self):
        # Guard the mode the first-run writer asks the kernel for.
        self.assertEqual(stat.S_IRUSR | stat.S_IWUSR, 0o600)


if __name__ == "__main__":
    unittest.main()
