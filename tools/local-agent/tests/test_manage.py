import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("manager", HERE / "manage.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
runner = manager.runner


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.remote = self.home / "remote"
        self.source = self.home / "source"
        self.git("init", "-b", "main", str(self.remote))
        self.git("config", "user.email", "test@example.com", cwd=self.remote)
        self.git("config", "user.name", "Test", cwd=self.remote)
        (self.remote / "tracked.txt").write_text("base\n")
        (self.remote / ".gitignore").write_text("ignored\n")
        self.git("add", ".", cwd=self.remote)
        self.git("commit", "-m", "base", cwd=self.remote)
        self.git("clone", str(self.remote), str(self.source))
        self.git("config", "user.email", "test@example.com", cwd=self.source)
        self.git("config", "user.name", "Test", cwd=self.source)
        self.path = manager.task_dir(self.home, "sample")
        self.path.mkdir(parents=True)
        self.work = self.path / "checkout"
        self.git("worktree", "add", "-b", "agent/sample", str(self.work), "HEAD", cwd=self.source)
        head = self.git("rev-parse", "HEAD", cwd=self.work)
        runner.save(self.path / "state.json", {"repo": "owner/repo", "base": "main",
                    "base_sha": head, "branch": "agent/sample", "status": "no_changes",
                    "attempt": 1, "worktree_source": str(self.source)})

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def test_installer_creates_working_launcher_and_refuses_overwrite(self):
        tooling = self.remote / "tools/local-agent"
        tooling.mkdir(parents=True)
        for filename in ("manage.py", "run.py", "evidence.py"):
            (tooling / filename).write_text((HERE / filename).read_text())
        self.git("add", ".", cwd=self.remote)
        self.git("commit", "-m", "tooling", cwd=self.remote)
        target = self.home / "installed"
        bin_dir = self.home / "bin with spaces"
        original = runner.command

        def clone_local(argv, cwd=None, timeout=120):
            argv = [str(self.remote) if arg == "https://github.com/ILGSupreme/Orin.git"
                    else arg for arg in argv]
            return original(argv, cwd, timeout)

        with patch.object(runner, "command", side_effect=clone_local):
            manager.install(target, "main", bin_dir)
        output = subprocess.check_output([str(bin_dir / "orin-dev"), "list"], text=True)
        self.assertIn(str(target), output)
        with self.assertRaisesRegex(ValueError, "already initialized"):
            manager.install(target, "main", bin_dir)

    def test_worktrees_share_storage_with_independent_files(self):
        (self.work / "tracked.txt").write_text("task\n")
        self.assertEqual((self.source / "tracked.txt").read_text(), "base\n")
        common = self.git("rev-parse", "--git-common-dir", cwd=self.work)
        self.assertEqual(Path(common), self.source / ".git")

    def test_archive_clean_no_change_retains_records_and_branch(self):
        (self.path / "evidence.json").write_text("{}")
        manager.archive(self.home, self.path, confirmed=True)
        self.assertFalse(self.work.exists())
        self.assertTrue((self.path / "evidence.json").exists())
        self.assertEqual(manager.state_for(self.path)["status"], "archived")
        self.git("rev-parse", "agent/sample", cwd=self.source)

    def test_archive_preserves_blocked_dirty_work_and_index(self):
        state = manager.state_for(self.path)
        state.update(status="blocked", reason="verifier failed")
        runner.save(self.path / "state.json", state)
        (self.work / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt", cwd=self.work)
        (self.work / "tracked.txt").write_text("unstaged\n")
        (self.work / "untracked").write_bytes(b"\x00untracked")
        (self.work / "ignored").write_text("ignored data")
        (self.work / "link").symlink_to("tracked.txt")
        before = self.git("status", "--porcelain", "--ignored", cwd=self.work)
        manager.archive(self.home, self.path, confirmed=True)
        saved = self.path / "archive/checkout"
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=saved), before)
        self.assertEqual(self.git("show", ":tracked.txt", cwd=saved), "staged")
        self.assertEqual((saved / "tracked.txt").read_text(), "unstaged\n")
        self.assertEqual((saved / "untracked").read_bytes(), b"\x00untracked")
        self.assertEqual((saved / "ignored").read_text(), "ignored data")
        self.assertTrue((saved / "link").is_symlink())
        archived = manager.state_for(self.path)
        self.assertEqual(archived["archived_from"], "blocked")
        self.assertEqual(archived["reason"], "verifier failed")
        manifest = json.loads((self.path / "archive/manifest.json").read_text())
        self.assertEqual(manifest["original_state"], state)
        # Recover the complete working tree and index using ordinary Git.
        self.git("worktree", "move", str(saved), str(self.work), cwd=self.source)
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=self.work), before)
        self.assertEqual(self.git("show", ":tracked.txt", cwd=self.work), "staged")

    def test_unpublished_commit_recoverable_from_bundle(self):
        (self.work / "tracked.txt").write_text("unpublished")
        self.git("add", ".", cwd=self.work)
        self.git("commit", "-m", "unpublished", cwd=self.work)
        head = self.git("rev-parse", "HEAD", cwd=self.work)
        state = manager.state_for(self.path)
        state["status"] = "local_ready"
        runner.save(self.path / "state.json", state)
        manager.archive(self.home, self.path, confirmed=True)
        recovered = self.home / "recovered"
        self.git("clone", str(self.path / "archive/history.bundle"), str(recovered))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=recovered), head)
        self.assertEqual((recovered / "tracked.txt").read_text(), "unpublished")

    def test_archive_rejects_running_state(self):
        state = manager.state_for(self.path)
        state["status"] = "running"
        runner.save(self.path / "state.json", state)
        with self.assertRaisesRegex(ValueError, "idle"):
            manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())

    def test_archive_cancel_keeps_checkout(self):
        with patch("builtins.input", return_value="n"):
            manager.archive(self.home, self.path)
        self.assertTrue(self.work.exists())
        self.assertFalse((self.path / "archive").exists())

    def test_archive_retries_after_state_write_failure(self):
        original = runner.save

        def fail_state(path, value):
            if path.name == "state.json":
                raise OSError("simulated interruption")
            original(path, value)

        with patch.object(runner, "save", side_effect=fail_state):
            with self.assertRaises(OSError):
                manager.archive(self.home, self.path, confirmed=True)
        self.assertFalse(self.work.exists())
        self.assertTrue((self.path / "archive/checkout/tracked.txt").exists())
        manager.archive(self.home, self.path, confirmed=True)
        self.assertEqual(manager.state_for(self.path)["status"], "archived")

    def test_archive_retries_failed_move_and_detects_bundle_corruption(self):
        original = runner.command

        def fail_move(argv, cwd=None, timeout=120):
            if argv[:3] == ["git", "worktree", "move"]:
                raise RuntimeError("simulated move failure")
            return original(argv, cwd, timeout)

        with patch.object(runner, "command", side_effect=fail_move):
            with self.assertRaises(RuntimeError):
                manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())
        bundle = self.path / "archive/history.bundle"
        original_bytes = bundle.read_bytes()
        bundle.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())
        bundle.write_bytes(original_bytes)
        manager.archive(self.home, self.path, confirmed=True)
        self.assertEqual(manager.state_for(self.path)["status"], "archived")

    def test_archive_rejects_foreign_repository(self):
        self.git("worktree", "remove", str(self.work), cwd=self.source)
        self.git("clone", str(self.remote), str(self.work))
        self.git("switch", "-c", "agent/sample", cwd=self.work)
        with self.assertRaisesRegex(ValueError, "different Git repository"):
            manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())

    def test_archive_bundle_failure_keeps_original(self):
        original = runner.command

        def fail_bundle(argv, cwd=None, timeout=120):
            if argv[:3] == ["git", "bundle", "verify"]:
                raise RuntimeError("simulated verification failure")
            return original(argv, cwd, timeout)

        with patch.object(runner, "command", side_effect=fail_bundle):
            with self.assertRaises(RuntimeError):
                manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())
        self.assertEqual(manager.state_for(self.path)["status"], "no_changes")

    def test_archive_rejects_busy_run(self):
        with (self.path / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                manager.archive(self.home, self.path, confirmed=True)
        self.assertTrue(self.work.exists())

    def test_published_archive_after_merge_and_remote_branch_deletion(self):
        (self.work / "tracked.txt").write_text("candidate")
        self.git("add", ".", cwd=self.work)
        self.git("commit", "-m", "candidate", cwd=self.work)
        head = self.git("rev-parse", "HEAD", cwd=self.work)
        self.git("push", "origin", "HEAD:refs/heads/agent/sample", cwd=self.work)
        self.git("merge", "--squash", "agent/sample", cwd=self.remote)
        self.git("commit", "-m", "squashed candidate", cwd=self.remote)
        self.git("push", "origin", "--delete", "agent/sample", cwd=self.work)
        state = manager.state_for(self.path)
        state.update(status="awaiting_human", candidate=head)
        runner.save(self.path / "state.json", state)
        manager.archive(self.home, self.path, confirmed=True)
        self.assertFalse(self.work.exists())
        archived = manager.state_for(self.path)
        self.assertEqual(archived["archived_head"], head)
        self.assertEqual(archived["archived_from"], "awaiting_human")
        manifest = json.loads((self.path / "archive/manifest.json").read_text())
        self.assertFalse(manifest["merge_verified"])

    def test_archive_rejects_wrong_branch_and_symlink(self):
        self.git("switch", "-c", "unexpected", cwd=self.work)
        with self.assertRaisesRegex(ValueError, "unexpected branch"):
            manager.archive(self.home, self.path, confirmed=True)
        self.git("switch", "agent/sample", cwd=self.work)
        (self.path / "archive").symlink_to(self.home / "elsewhere")
        with self.assertRaisesRegex(ValueError, "redirected"):
            manager.archive(self.home, self.path, confirmed=True)

    def test_names_cannot_escape_home(self):
        for name in ("../elsewhere", "/tmp/task", "a/b", "..", "", "-bad"):
            with self.assertRaises(ValueError):
                manager.task_dir(self.home, name)
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / "runs/link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            manager.task_dir(self.home, "link")

    def test_archived_run_cannot_recreate_checkout(self):
        manager.archive(self.home, self.path, confirmed=True)
        args = argparse.Namespace(run=self.path, feedback=None, repo="owner/repo")
        with self.assertRaisesRegex(ValueError, "archived"):
            runner.run(args)
        self.assertFalse(self.work.exists())

    def test_runner_prepares_real_shared_worktree(self):
        path = self.home / "runs/new-task"
        task = self.home / "task.md"
        checks = self.home / "checks.json"
        task.write_text("Inspect the tracked file")
        checks.write_text(json.dumps([[sys.executable, "-c", "pass"]]))
        args = argparse.Namespace(run=path, task=task, checks=checks, repo="owner/repo",
                                  base="main", feedback=None, worktree_source=self.source,
                                  prepare=True, publish=False)
        original = runner.command

        def local_remote(argv, cwd=None, timeout=120):
            if argv == ["git", "remote", "get-url", "origin"]:
                return "https://github.com/owner/repo.git"
            return original(argv, cwd, timeout)

        with patch.object(runner, "command", side_effect=local_remote):
            self.assertEqual(runner.run(args), 0)
        state = manager.state_for(path)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["worktree_source"], str(self.source))
        self.assertTrue((path / "checkout/.git").is_file())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "main")


if __name__ == "__main__":
    unittest.main()
