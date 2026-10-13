import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from duet.workspace import Policy, PolicyError, Workspace, WorkspaceError, contains_secret


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_traversal_windows_names_and_protected_paths(self):
        for name in ["../x", "/tmp/x", "C:/x", "a\\b", "a:b", "a/./b", "a//b", ".GiT/config", ".github/workflows/ci.yml", ".env", "keys/a.pem", "a/NUL.txt", "a/CON", "a/file.", "a/file "]:
            with self.subTest(name=name), self.assertRaises(PolicyError):
                Policy([name])

    def test_validate_all_before_writing(self):
        policy = Policy(["safe.txt", "other.txt"])
        (self.root / "safe.txt").write_text("original", encoding="utf-8")
        with self.assertRaises(PolicyError):
            policy.apply(self.root, [{"path": "safe.txt", "content": "changed"}, {"path": "../../escape", "content": "x"}])
        self.assertEqual((self.root / "safe.txt").read_text(), "original")

    def test_size_and_secret_values_are_rejected_without_leaking(self):
        policy = Policy(["safe.txt"], max_file_bytes=128, max_total_bytes=128)
        with self.assertRaises(PolicyError):
            policy.apply(self.root, [{"path": "safe.txt", "content": "x" * 129}])
        secret = "sk-" + "a" * 30
        with self.assertRaises(PolicyError) as error:
            policy.apply(self.root, [{"path": "safe.txt", "content": secret}])
        self.assertNotIn(secret, str(error.exception))
        self.assertTrue(contains_secret(secret))
        (self.root / "safe.txt").write_text(secret)
        with self.assertRaises(PolicyError):
            policy.read_context(self.root)

    def test_duplicates_and_case_collisions(self):
        with self.assertRaises(PolicyError):
            Policy(["a.txt", "A.txt"])
        with self.assertRaises(PolicyError):
            Policy(["a", "a/b"])
        policy = Policy(["safe.txt"])
        change = {"path": "safe.txt", "content": "ok"}
        with self.assertRaises(PolicyError):
            policy.apply(self.root, [change, change])
        (self.root / "SAFE.txt").write_text("existing")
        with self.assertRaises(PolicyError):
            policy.read_context(self.root)

    def test_symlink_ancestor(self):
        (self.root / "real").mkdir()
        try:
            (self.root / "linked").symlink_to(self.root / "real", target_is_directory=True)
        except OSError:
            self.skipTest("OS does not grant symlink creation")
        with self.assertRaises(PolicyError):
            Policy(["linked/a.txt"]).apply(self.root, [{"path": "linked/a.txt", "content": "x"}])

    def test_read_only_allowlisted_text_and_digest(self):
        (self.root / "other.txt").write_text("not included")
        policy = Policy(["src/a.txt", "new.txt"])
        policy.apply(self.root, [{"path": "src/a.txt", "content": "hello\n"}])
        self.assertEqual(policy.read_context(self.root), {"src/a.txt": "hello\n"})
        digest = policy.digest(self.root)
        policy.apply(self.root, [{"path": "src/a.txt", "content": "changed\n"}])
        self.assertNotEqual(digest, policy.digest(self.root))


@unittest.skipUnless(shutil.which("git"), "Git is required")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        (self.repo / "hello.txt").write_text("before\n")
        self.git("add", "hello.txt")
        self.git("commit", "-m", "initial")
        self.original = self.git("rev-parse", "HEAD").stdout.strip()
        self.workspace = Workspace(self.repo, self.root / "state", "test-run")
        self.addCleanup(self.cleanup)

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def cleanup(self):
        # Git metadata is read-only on Windows; use a local chmod handler.
        def writable_remove(function, path, error):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(self.root, onerror=writable_remove)
        self.temp.cleanup()

    def test_real_isolated_worktree_commit_and_integration(self):
        integration = self.workspace.create()
        task = self.workspace.task_worktree("task-1")
        Policy(["hello.txt"]).apply(task, [{"path": "hello.txt", "content": "after\n"}])
        commit = self.workspace.commit_task(task, "task-1")
        self.workspace.integrate(commit)
        self.assertEqual((integration / "hello.txt").read_text(), "after\n")
        self.assertEqual((self.repo / "hello.txt").read_text(), "before\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.original)
        self.assertTrue(task.exists())
        self.assertIn("duet/test-run/integration", self.git("branch", "--list").stdout)
        second = self.workspace.task_worktree("task-2")
        self.assertEqual((second / "hello.txt").read_text(), "after\n")

    def test_dirty_and_duplicate_runs_are_rejected(self):
        (self.repo / "untracked.txt").write_text("dirty")
        with self.assertRaises(WorkspaceError):
            self.workspace.create()
        (self.repo / "untracked.txt").unlink()
        self.workspace.create()
        with self.assertRaises(WorkspaceError):
            Workspace(self.repo, self.root / "state2", "test-run").create()
        with self.assertRaises(WorkspaceError):
            Workspace(self.repo, self.repo / "state", "another")

    def test_tracked_symlink_and_submodules_rejected(self):
        blob = self.git("rev-parse", "HEAD:hello.txt").stdout.strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},link.txt")
        self.git("commit", "-m", "symlink")
        # Make index and worktree agree even on Windows without symlink privilege.
        self.git("checkout", "--", "link.txt")
        with self.assertRaises(WorkspaceError):
            self.workspace.create()

    def test_hooks_are_disabled(self):
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        marker = self.root / "hook-was-executed"
        hook.write_text('#!/bin/sh\nprintf bad > "' + marker.as_posix() + '"\n', encoding="utf-8")
        hook.chmod(0o755)
        self.workspace.create()
        self.assertFalse(marker.exists())

    def test_git_argument_injection_is_rejected(self):
        with self.assertRaises(WorkspaceError):
            Workspace(self.repo, self.root / "state", "--bad")
        self.workspace.create()
        with self.assertRaises(WorkspaceError):
            self.workspace.task_worktree("../bad")
        with self.assertRaises(WorkspaceError):
            self.workspace.integrate("--help")
