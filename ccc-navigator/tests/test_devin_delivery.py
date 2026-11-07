import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devin_delivery as d


class DeliveryTests(unittest.TestCase):
    def run_case(self, owners, send_error=False):
        calls = []
        def run(args, timeout=15):
            calls.append(args)
            if args[1] == 'list':
                return {'terminals': [{'handle': k, 'ptyId': k, 'agentIdentity': 'devin', 'connected': True, 'writable': True} for k in owners]}
            if send_error:
                raise RuntimeError('connection lost')
            return {'send': {'accepted': True}}
        with patch.object(d, 'lock_holder', return_value=123), patch.object(d, 'process_ancestors', return_value={123, 100}), patch.object(d, 'terminal_processes', return_value=owners), patch.object(d, 'run', side_effect=run), patch.object(d.subprocess, 'run', return_value=Mock(stdout='ttys013', returncode=0)):
            result = d.deliver('devincli-example', 'hello', '', [{'session_id': 'devincli-example', 'session_cwd': '/work'}])
        return result, calls

    def test_only_the_owning_terminal_receives_input(self):
        result, calls = self.run_case({'right': 100, 'other': 101})
        self.assertTrue(result['ok'])
        self.assertTrue(result['queued'])
        sends = [x for x in calls if x[1] == 'send']
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][3], 'right')

    def test_duplicate_or_missing_owner_never_sends(self):
        for owners in ({'a': 100, 'b': 100}, {'a': 101}):
            result, calls = self.run_case(owners)
            self.assertFalse(result['ok'])
            self.assertFalse(any(x[1] == 'send' for x in calls))

    def test_uncertain_transport_is_not_retried(self):
        result, calls = self.run_case({'right': 100}, send_error=True)
        self.assertTrue(result['queued'])
        self.assertIn('Check Orca', result['warning'])
        self.assertEqual(sum(x[1] == 'send' for x in calls), 1)

    def test_unlocked_session_uses_existing_route(self):
        with patch.object(d, 'lock_holder', return_value=None):
            self.assertIsNone(d.deliver('devincli-example', 'hello', '', []))
