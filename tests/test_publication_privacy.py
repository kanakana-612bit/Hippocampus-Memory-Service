"""Privacy regressions using only fictional Git objects and generated format headers."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from check_commit_privacy import check_object, push, require_ident, require_message
from check_public_tree import check_tree, content_findings, path_findings

SAFE = "fictional-owner@users.noreply.github.com"
BAD = "fictional-owner@example.invalid"
CONTENT_BAD = "fictional" + "@" + "synthetic-recipient.example"


class PublicationPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fictional-git-")
        self.root = Path(self.temp.name)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Fiction", "GIT_COMMITTER_NAME": "Fiction",
                    "GIT_AUTHOR_EMAIL": SAFE, "GIT_COMMITTER_EMAIL": SAFE,
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            self.env.pop(key, None)
        self.git("init", "-q", "-b", "main")
        self.tree = self.git("mktree", data=b"").decode().strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, data=None, env=None):
        return subprocess.check_output(["git", *args], cwd=self.root, input=data,
                                       env=env or self.env, stderr=subprocess.PIPE)

    def commit(self, author=SAFE, committer=SAFE, parent=None, message="Fictional test\n", tree=None, name="Fiction"):
        env = {**self.env, "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_EMAIL": committer,
               "GIT_AUTHOR_NAME": name}
        return self.git("commit-tree", tree or self.tree, *(["-p", parent] if parent else []),
                        data=message.encode(), env=env).decode().strip()

    def tag(self, target, email=SAFE, message="Fictional tag", kind="commit"):
        raw = f"object {target}\ntype {kind}\ntag fictional\ntagger Fiction <{email}> 1000000000 +0000\n\n{message}\n"
        return self.git("hash-object", "-t", "tag", "-w", "--stdin", data=raw.encode()).decode().strip()

    def stage(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.git("add", "--", path)
        return self.git("write-tree").decode().strip()

    def test_valid_and_invalid_commit_identities(self):
        self.assertEqual(check_object(self.root, self.commit()), 1)
        for kwargs in ({"author": BAD}, {"committer": BAD}, {"name": BAD}):
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(ValueError):
                    check_object(self.root, self.commit(**kwargs))

    def test_old_ancestor_is_not_hidden_by_clean_tip(self):
        with self.assertRaises(ValueError):
            check_object(self.root, self.commit(parent=self.commit(author=BAD)))

    def test_trailers_and_tag_messages(self):
        sha = self.commit()
        for ref in (self.commit(message="Co-authored-by: Fiction <" + BAD + ">"),
                    self.tag(sha, email=BAD), self.tag(sha, message=BAD),
                    self.tag(self.tag(sha, message=BAD), kind="tag")):
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    check_object(self.root, ref)
        self.assertEqual(check_object(self.root, self.tag(sha)), 1)

    def test_actual_staged_content_not_worktree(self):
        self.stage("fiction.txt", CONTENT_BAD.encode())
        (self.root / "fiction.txt").write_text("safe fictional text")
        with self.assertRaises(RuntimeError):
            check_tree(self.root, None, "main")
        self.stage("fiction.txt", b"safe fictional text")
        (self.root / "fiction.txt").write_text(CONTENT_BAD)
        self.assertEqual(check_tree(self.root, None, "main"), 1)

    def test_deleted_private_content_in_ancestor(self):
        bad = self.commit(tree=self.stage("fiction.txt", CONTENT_BAD.encode()))
        with self.assertRaises(RuntimeError):
            check_object(self.root, self.commit(parent=bad))

    def test_large_binary_and_lfs_are_not_silently_skipped(self):
        values = [b"x" * 2_000_001 + b"\n" + CONTENT_BAD.encode(), b"\xff\xfe",
                  b"fiction\0", b"version https://git-lfs.github.com/spec/v1\n"]
        for data in values:
            with self.subTest(length=len(data)):
                self.assertTrue(content_findings(data, "fiction.txt"))

    def test_media_and_wrapped_base64_detection(self):
        # A generated, incomplete format header; no recording or speaker data.
        wav = b"RIFF" + (44).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"\0" * 40
        encoded = base64.b64encode(wav).decode()
        wrapped = "\n".join(encoded[i:i + 16] for i in range(0, len(encoded), 16))
        for data, name in [(wav, "fiction.txt"), (encoded.encode(), "fiction.txt"),
                           (json.dumps({"fiction": wrapped}).encode(), "fiction.json"),
                           (repr(wav).encode(), "fiction.py")]:
            with self.subTest(name=name, length=len(data)):
                self.assertTrue(content_findings(data, name))

    def test_runtime_and_documentation_paths_have_no_blanket_exemption(self):
        for path in ("recordings/fiction.txt", "docs/assets/fiction.wav", ".env.local", "fiction.m4a", CONTENT_BAD + ".txt"):
            self.assertTrue(path_findings(path))
        self.assertTrue(content_findings(CONTENT_BAD.encode(), "scripts/check_public_tree.py"))

    def test_fictional_content_and_long_identifier_pass(self):
        self.assertEqual(content_findings(json.dumps({"fiction": BAD}).encode(), "fiction.json"), [])
        identifier = "test_" + "fictional_feature_" * 8
        self.assertEqual(content_findings(("def " + identifier + "(): pass\n").encode(), "fiction.py"), [])

    def test_symlinks_and_submodules_are_rejected(self):
        oid = self.git("hash-object", "-w", "--stdin", data=b"fiction").decode().strip()
        for mode, kind, target in [("120000", "blob", oid), ("160000", "commit", self.commit())]:
            tree = self.git("mktree", data=f"{mode} {kind} {target}\tfiction\n".encode()).decode().strip()
            with self.assertRaises(RuntimeError):
                check_tree(self.root, tree, "main")

    def test_replace_ref_does_not_hide_original_identity(self):
        bad, safe = self.commit(author=BAD), self.commit()
        self.git("replace", bad, safe)
        with self.assertRaises(ValueError):
            check_object(self.root, bad)

    def test_shallow_history_rejected(self):
        sha = self.commit()
        (self.root / ".git/shallow").write_text(sha + "\n")
        with self.assertRaises(ValueError):
            check_object(self.root, sha)

    def test_grafted_history_rejected(self):
        sha = self.commit()
        (self.root / ".git/info/grafts").write_text(sha + "\n")
        with self.assertRaises(ValueError):
            check_object(self.root, sha)

    def test_push_refs_and_delete(self):
        sha = self.commit()
        push(self.root, [f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"])
        push(self.root, [f"(delete) {'0' * 40} refs/heads/main {sha}\n"])
        for target in ("refs/notes/fiction", "refs/replace/fiction", "refs/heads/" + BAD):
            with self.assertRaises(ValueError):
                push(self.root, [f"HEAD {sha} {target} {'0' * 40}\n"])

    def test_real_hooks_reject_before_commit_and_push(self):
        (self.root / "scripts").mkdir()
        for name in ("check_public_tree.py", "check_commit_privacy.py", "install_publication_hooks.py"):
            shutil.copyfile(SCRIPTS / name, self.root / "scripts" / name)
        subprocess.run([sys.executable, str(self.root / "scripts/install_publication_hooks.py"),
                        "--name", "Fiction", "--email", SAFE], cwd=self.root, env=self.env,
                       check=True, capture_output=True)
        self.git("add", "scripts")
        attempts = [({**self.env, "GIT_COMMITTER_EMAIL": BAD}, "Fictional"),
                    (self.env, "Fictional " + BAD)]
        for env, message in attempts:
            result = subprocess.run(["git", "commit", "-m", message], cwd=self.root,
                                    env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Publication blocked:", result.stderr)
            self.assertNotIn(BAD.encode(), result.stderr)
        result = subprocess.run(["git", "commit", "-qm", "Fictional safe publication"],
                                cwd=self.root, env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        safe_head = self.git("rev-parse", "HEAD").decode().strip()
        # commit-tree bypasses creation hooks; pre-push must still reject it.
        bad = self.commit(committer=BAD, parent=safe_head)
        self.git("update-ref", "refs/heads/main", bad)
        remote = self.root / "fictional-remote.git"
        self.git("init", "--bare", "-q", str(remote))
        result = subprocess.run(["git", "push", str(remote), "main"], cwd=self.root,
                                env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Publication blocked:", result.stderr)
        self.assertFalse((remote / "refs/heads/main").exists())
        self.git("update-ref", "refs/heads/main", safe_head)
        self.git("push", str(remote), "main")


if __name__ == "__main__":
    unittest.main()
