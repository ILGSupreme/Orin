import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("runner", Path(__file__).parents[1] / "run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_rejects_shell_string_as_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checks.json"
            path.write_text('["echo ok; exit 0"]')
            with self.assertRaises(ValueError):
                runner.checks_from(path)

    def test_nonzero_check_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                runner.execute([sys.executable, "-c", "raise SystemExit(3)"],
                               tmp, Path(tmp) / "log", 5)

    def test_timeout_stops_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.execute([sys.executable, "-c", "import time; time.sleep(60)"],
                               tmp, Path(tmp) / "log", 0.05)

    def scenario(self, result_status="ready", fail_check=False, publish=True, existing=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        work = root / "checkout"
        work.mkdir()
        runner.save(root / "state.json", {
            "repo": "owner/repo", "base": "main", "branch": "agent/test",
            "base_sha": "base", "attempt": 0, "task": "fix a bug",
            "checks": [[sys.executable, "-m", "unittest"]], "status": "created"})
        feedback = root / "feedback.txt"
        feedback.write_text("continue")
        args = argparse.Namespace(run=root, repo="owner/repo", feedback=feedback,
                                  publish=publish, minutes=1, check_minutes=1)
        calls = []

        def command(argv, cwd=None, timeout=120):
            calls.append(argv)
            if argv[:3] == ["git", "branch", "--show-current"]:
                return "agent/test"
            if argv[:3] == ["gh", "pr", "list"]:
                return json.dumps([{"url": "https://github.com/owner/repo/pull/3",
                                    "state": "OPEN", "isDraft": True}] if existing else [])
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return "candidate"
            if argv[:2] == ["git", "diff"]:
                return "change.py" if "--check" not in argv else ""
            if argv[:3] == ["gh", "pr", "create"]:
                return "https://github.com/owner/repo/pull/3"
            return ""

        def execute(argv, cwd, log, timeout, stdin=None):
            if argv[0] == "codex":
                runner.save(Path(argv[argv.index("-o") + 1]),
                            {"status": result_status, "title": "Fix bug", "body": "Evidence"})
            elif fail_check:
                raise RuntimeError("test failed")

        with patch.object(runner, "command", side_effect=command), \
                patch.object(runner, "execute", side_effect=execute):
            try:
                runner.run(args)
            except RuntimeError:
                if not fail_check:
                    raise
        return calls, json.loads((root / "state.json").read_text())

    def test_blocked_agent_cannot_publish(self):
        calls, state = self.scenario(result_status="blocked")
        self.assertEqual(state["status"], "blocked")
        self.assertFalse(any(c[:2] == ["git", "push"] for c in calls))

    def test_failed_checks_cannot_publish(self):
        calls, state = self.scenario(fail_check=True)
        self.assertEqual(state["status"], "blocked")
        self.assertFalse(any(c[:2] == ["git", "push"] for c in calls))

    def test_success_creates_only_draft(self):
        calls, state = self.scenario()
        create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual(state["status"], "awaiting_human")
        self.assertEqual(state["candidate"], "candidate")

    def test_feedback_updates_existing_pr(self):
        calls, state = self.scenario(existing=True)
        self.assertTrue(any(c[:3] == ["gh", "pr", "edit"] for c in calls))
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in calls))
        self.assertEqual(state["status"], "awaiting_human")

    def test_local_mode_never_pushes(self):
        calls, state = self.scenario(publish=False)
        self.assertFalse(any(c[0] == "gh" or c[:2] == ["git", "push"] for c in calls))
        self.assertEqual(state["status"], "local_ready")

    def test_invalid_result_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            runner.save(path, {"status": "ready", "title": "ok"})
            with self.assertRaises(ValueError):
                runner.result_from(path)


if __name__ == "__main__":
    unittest.main()
