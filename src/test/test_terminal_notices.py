import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class TerminalNoticesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / "terminal-chat.py"
        spec = importlib.util.spec_from_file_location("orin_terminal_test", path)
        cls.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.client)

    def test_background_notices_are_scoped_and_not_duplicated(self):
        values = [
            {
                "id": "goal:1",
                "notices": [
                    {"id": "n1", "text": "older"},
                    {"id": "n2", "text": "latest"},
                ],
            }
        ]
        response = SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"goals": values}
        )
        seen = {}
        with patch.object(self.client.requests, "get", return_value=response) as get:
            first = self.client.collect_goal_notices("session-1", seen)
            self.assertEqual(first, [("goal:1", "n2", "latest")])
            seen["goal:1"] = "n2"
            self.assertEqual(self.client.collect_goal_notices("session-1", seen), [])
            values[0]["notices"].append({"id": "n3", "text": "new result"})
            self.assertEqual(
                self.client.collect_goal_notices("session-1", seen),
                [("goal:1", "n3", "new result")],
            )
            self.assertEqual(get.call_args.kwargs["params"]["session_id"], "session-1")
            self.assertEqual(
                get.call_args.kwargs["params"]["user_id"], self.client.USER_ID
            )

    def test_expired_notice_cursor_recovers_latest_result(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "goals": [
                    {"id": "goal:1", "notices": [{"id": "new", "text": "current"}]},
                ]
            },
        )
        with patch.object(self.client.requests, "get", return_value=response):
            values = self.client.collect_goal_notices("s", {"goal:1": "expired"})
        self.assertEqual(values, [("goal:1", "new", "current")])
