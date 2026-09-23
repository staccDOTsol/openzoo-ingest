"""The engine is trusted only when the worktree matches a pinned commit."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "daemon" / "verify_engine.py"
REQUIRED = (
    "CAPABILITIES.md",
    "holographic_service.py",
    "holographic/agents_and_reasoning/holographic_ai.py",
    "holographic/caching_and_storage/holographic_index.py",
    "holographic/caching_and_storage/holographic_knowledgestore.py",
    "holographic/mesh_and_geometry/holographic_planshape.py",
    "holographic/misc/holographic_determinism.py",
    "holographic/misc/holographic_superposed.py",
)


def run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def git_ident(repo: Path):
    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "engine-verify@example.com"], repo)
    run(["git", "config", "user.name", "engine-verify"], repo)


def commit_all(repo: Path, message: str) -> str:
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "-m", message], repo)
    return run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def write_engine(repo: Path, extra: str = "extra = 1\n"):
    for rel in REQUIRED:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# %s\n" % rel)
    (repo / "extra.py").write_text(extra)


def verify(repo: Path, commit: str):
    return subprocess.run(
        ["python3", str(VERIFY), "--root", str(repo), "--commit", commit],
        capture_output=True,
        text=True,
    )


class VerifyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "leCore"
        self.repo.mkdir()
        git_ident(self.repo)
        write_engine(self.repo)
        self.commit = commit_all(self.repo, "engine")

    def test_exact_tree_is_trusted(self):
        result = verify(self.repo, self.commit)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_filename_without_the_commit_is_not_enough(self):
        planted = Path(self.tmp.name) / "planted"
        target = planted / "holographic" / "caching_and_storage"
        target.mkdir(parents=True)
        (target / "holographic_index.py").write_text("print('not the engine')\n")
        result = verify(planted, self.commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a git checkout", result.stderr)

    def test_modified_tracked_file_is_rejected(self):
        path = self.repo / "holographic" / "caching_and_storage" / "holographic_index.py"
        path.write_text("TAMPERED = True\n")
        result = verify(self.repo, self.commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("holographic_index.py", result.stderr)

    def test_untracked_file_is_rejected(self):
        (self.repo / "holographic" / "__init__.py").write_text("raise SystemExit('injected')\n")
        result = verify(self.repo, self.commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untracked", result.stderr)

    def test_wrong_commit_is_rejected(self):
        (self.repo / "extra.py").write_text("extra = 2\n")
        later = commit_all(self.repo, "later")
        self.assertNotEqual(later, self.commit)
        result = verify(self.repo, self.commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD", result.stderr)

    def test_branch_name_is_not_a_pin(self):
        result = verify(self.repo, "main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("40-character", result.stderr)

    def test_missing_required_file_in_the_commit_is_rejected(self):
        thin = Path(self.tmp.name) / "thin"
        thin.mkdir()
        git_ident(thin)
        (thin / "holographic" / "caching_and_storage").mkdir(parents=True)
        (thin / "holographic" / "caching_and_storage" / "holographic_index.py").write_text("x\n")
        sha = commit_all(thin, "thin")
        result = verify(thin, sha)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)


class PinFileTests(unittest.TestCase):
    def test_numpy_pin_is_exact_and_run_sh_uses_it(self):
        req = (ROOT / "daemon" / "requirements.txt").read_text()
        pins = [
            line.strip() for line in req.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(pins, ["numpy==2.5.3"])
        script = (ROOT / "daemon" / "run.sh").read_text()
        self.assertNotIn("pip install --quiet --disable-pip-version-check numpy\n", script)
        self.assertNotIn('pip install --disable-pip-version-check --only-binary=numpy "numpy==$pin"', script)
        self.assertIn('numpy==$pin', script)
        self.assertIn("--only-binary=numpy", script)
        self.assertIn("--only-binary=:all:", script)
        self.assertIn("--require-hashes", script)
        self.assertIn("--no-deps", script)
        self.assertIn("requirements.lock", script)
        self.assertIn("verify_engine.py", script)
        self.assertIn("ea456c3f1d4b05acc6ac1ea5a9946d77759f8d72", script)
        lock = (ROOT / "daemon" / "requirements.lock").read_text()
        self.assertIn("numpy==2.5.3 \\\n", lock)
        self.assertNotIn("numpy-2.5.3.tar.gz", lock)
        # The published sdist digest must not be an accepted artifact.
        self.assertNotIn(
            "df2d5874ff183595a4ba404edd04f6bd9b5505c1d7708573f6a6c17489a67563",
            lock,
        )
        wheel_names = []
        wheel_hashes = []
        requirement_hashes = []
        for line in lock.splitlines():
            if line.startswith("# numpy-"):
                name, digest = line[2:].split()
                self.assertTrue(name.endswith(".whl"))
                self.assertNotIn(name, wheel_names)
                wheel_names.append(name)
                wheel_hashes.append(digest)
            stripped = line.strip()
            if stripped.endswith("\\"):
                stripped = stripped[:-1].strip()
            if stripped.startswith("--hash=sha256:"):
                digest = stripped[len("--hash=sha256:") :]
                self.assertEqual(len(digest), 64)
                self.assertEqual(digest, digest.lower())
                requirement_hashes.append(digest)
        self.assertGreaterEqual(len(wheel_names), 1)
        self.assertEqual(wheel_hashes, requirement_hashes)
        self.assertEqual(len(requirement_hashes), len(set(requirement_hashes)))


if __name__ == "__main__":
    unittest.main()
