import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
import evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / 'checkout'
        self.work.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.work)], check=True)
        (self.work / 'file.txt').write_text('reviewed contents')
        self.events = root / 'specialists.jsonl'
        self.log = root / 'agent.jsonl'
        self.log.write_text(json.dumps({'type': 'thread.started', 'thread_id': 'current'})+'\n')
        self.records = []
        for role in ('investigator', 'implementer', 'verifier'):
            base = dict(session_id='current', agent_id=role+'-id', agent_type=role, cwd=str(self.work))
            self.records.append(dict(base, hook_event_name='SubagentStart'))
            self.records.append(dict(base, hook_event_name='SubagentStop',
                report=json.dumps({'verdict': 'pass', 'summary': 'Completed assigned review',
                                   'evidence': ['Observed the requested behavior']}),
                snapshot=evidence.snapshot(self.work)))

    def validate(self):
        self.events.write_text(''.join(json.dumps(e)+'\n' for e in self.records))
        return evidence.validate(self.events, self.log, self.work)

    def test_complete_reports_pass(self):
        self.assertEqual(len(self.validate()['agents']), 3)

    def test_spawn_without_completion_fails(self):
        self.records.pop()
        with self.assertRaises(ValueError): self.validate()

    def test_other_attempt_fails(self):
        self.records[0]['session_id'] = 'previous'
        with self.assertRaises(ValueError): self.validate()

    def test_unknown_role_fails(self):
        self.records[0]['agent_type'] = 'default'
        with self.assertRaises(ValueError): self.validate()

    def test_coordinator_claim_is_not_report(self):
        self.records[-1]['report'] = 'All specialists passed'
        with self.assertRaises(ValueError): self.validate()

    def test_blocked_verifier_fails(self):
        report = json.loads(self.records[-1]['report']); report['verdict'] = 'blocked'
        self.records[-1]['report'] = json.dumps(report)
        with self.assertRaises(ValueError): self.validate()

    def test_review_before_worker_completion_fails(self):
        self.records[3], self.records[4] = self.records[4], self.records[3]
        with self.assertRaises(ValueError): self.validate()

    def test_edit_after_review_fails(self):
        (self.work / 'file.txt').write_text('unreviewed change')
        with self.assertRaises(ValueError): self.validate()

    def test_deleted_file_after_review_fails(self):
        (self.work / 'file.txt').unlink()
        with self.assertRaises(ValueError): self.validate()

    def test_foreign_checkout_fails(self):
        self.records[0]['cwd'] = str(self.work.parent)
        with self.assertRaises(ValueError): self.validate()

    def test_missing_file_fails(self):
        with self.assertRaises(ValueError):
            evidence.validate(self.events, self.log, self.work)

    def test_hook_captures_real_fingerprint(self):
        payload = copy.deepcopy(self.records[-1])
        payload['last_assistant_message'] = payload.pop('report')
        payload.pop('snapshot')
        evidence.capture(payload, self.events)
        saved = json.loads(self.events.read_text())
        self.assertEqual(saved['snapshot'], evidence.snapshot(self.work))
        self.assertEqual(json.loads(saved['report'])['verdict'], 'pass')


if __name__ == '__main__':
    unittest.main()
