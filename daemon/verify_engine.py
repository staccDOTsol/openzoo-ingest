#!/usr/bin/env python3
"""Fail closed unless a worktree is exactly one pinned commit.

The daemon executes this tree. A directory that merely contains one known
filename is not trusted: HEAD must be the pinned commit, every tracked blob
must match the worktree bytes, the entry points the daemon imports must be
present, and unexpected untracked files are refused.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Modules the sidecar imports. Presence in the commit is necessary, and the
# whole-tree hash below is what makes a swapped neighbor fail too.
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


def fail(msg: str) -> None:
    print("engine verify: " + msg, file=sys.stderr)
    raise SystemExit(1)


def git(root: str, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", root, *args],
        input=data,
        capture_output=True,
        check=False,
    )


def _ignored_untracked(path: str) -> bool:
    name = path.rstrip("/").split("/")[-1]
    return name == "__pycache__" or path.endswith(".pyc") or "/__pycache__/" in path


def _dirty_paths(raw: bytes) -> list[str]:
    """Parse `git status --porcelain -z` (rename records carry a second path)."""
    out: list[str] = []
    i = 0
    while i + 3 <= len(raw):
        xy = raw[i:i + 2]
        if raw[i + 2:i + 3] != b" ":
            fail("unexpected git status record")
        i += 3
        end = raw.find(b"\0", i)
        if end < 0:
            fail("truncated git status record")
        path = raw[i:end].decode()
        i = end + 1
        if xy[0:1] in b"RC":
            end2 = raw.find(b"\0", i)
            if end2 < 0:
                fail("truncated git status rename")
            path = raw[i:end2].decode()
            i = end2 + 1
        if not _ignored_untracked(path):
            out.append(path)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--commit", required=True)
    ns = parser.parse_args()
    root = os.path.abspath(ns.root)
    commit = ns.commit.strip().lower()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        fail("commit must be a 40-character sha1, not a branch name")
    git_entry = os.path.join(root, ".git")
    if not os.path.isdir(git_entry) and not os.path.isfile(git_entry):
        fail("%s is not a git checkout" % root)

    head = git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        fail("cannot read HEAD")
    actual = head.stdout.decode().strip().lower()
    if actual != commit:
        fail("HEAD %s != pinned %s" % (actual, commit))

    listed = git(root, "ls-tree", "-r", "-z", commit)
    if listed.returncode != 0:
        fail("pinned commit is not present in %s" % root)
    blobs: list[tuple[str, str, str]] = []
    for rec in listed.stdout.split(b"\0"):
        if not rec:
            continue
        try:
            meta, path_b = rec.split(b"\t", 1)
            mode, typ, sha = meta.decode().split()
            path = path_b.decode()
        except ValueError:
            fail("could not parse git ls-tree output")
        if typ != "blob":
            continue
        if "\n" in path:
            fail("refusing a path that contains a newline")
        blobs.append((mode, sha, path))
    if not blobs:
        fail("pinned commit has no files")
    by_path = {path: (mode, sha) for mode, sha, path in blobs}
    missing = [rel for rel in REQUIRED if rel not in by_path]
    if missing:
        fail("pinned commit is missing " + ", ".join(missing))

    regular: list[str] = []
    for mode, sha, path in blobs:
        full = os.path.join(root, path)
        if mode == "120000":
            if not os.path.islink(full):
                fail("%s is not the symlink recorded in %s" % (path, commit))
            target = os.readlink(full).encode()
            hashed = git(root, "hash-object", "--no-filters", "--stdin", data=target)
            if hashed.returncode != 0 or hashed.stdout.decode().strip() != sha:
                fail("%s does not match the pinned blob" % path)
            continue
        if os.path.islink(full) or not os.path.isfile(full):
            fail("missing %s" % path)
        regular.append(path)

    if regular:
        payload = "\n".join(regular).encode() + b"\n"
        hashed = git(root, "hash-object", "--no-filters", "--stdin-paths", data=payload)
        if hashed.returncode != 0:
            detail = hashed.stderr.decode(errors="replace").strip().splitlines()
            fail("worktree does not match %s (%s)" % (commit, detail[0] if detail else "hash-object failed"))
        got = [line.strip() for line in hashed.stdout.decode().splitlines() if line.strip()]
        if len(got) != len(regular):
            fail("hashed %d files, expected %d" % (len(got), len(regular)))
        bad = [rel for rel, digest in zip(regular, got) if digest != by_path[rel][1]]
        if bad:
            shown = ", ".join(bad[:8])
            extra = "" if len(bad) <= 8 else " (+%d more)" % (len(bad) - 8)
            fail("worktree differs from %s: %s%s" % (commit, shown, extra))

    status = git(root, "status", "--porcelain", "-uall", "-z")
    if status.returncode != 0:
        fail("git status failed")
    dirty = _dirty_paths(status.stdout)
    if dirty:
        fail("untracked or modified files: " + ", ".join(dirty[:8]))


if __name__ == "__main__":
    main()
