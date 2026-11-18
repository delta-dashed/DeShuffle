import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from ops.dispatch_health import decision, tick, WORKFLOW


class DispatcherTests(unittest.TestCase):
    now = 1800000000

    def run_at(self, age=0, status='completed', conclusion='success', branch='main'):
        return {'created_at': datetime.fromtimestamp(self.now - age, timezone.utc).isoformat(),
                'head_branch': branch, 'status': status, 'conclusion': conclusion}

    def test_dispatch_when_native_schedule_never_started(self):
        request = Mock(side_effect=[{'workflow_runs': []}, {}])
        self.assertEqual(tick('private', request=request, now=lambda: self.now), 'dispatch')
        request.assert_called_with('private', 'POST', WORKFLOW + '/dispatches',
                                   {'ref': 'main', 'inputs': {'trigger_source': 'external-timer'}})

    def test_five_minute_boundary_allows_runner_startup_jitter(self):
        self.assertEqual(decision([self.run_at(290)], self.now), 'dispatch')

    def test_fresh_failures_are_not_hidden_by_immediate_retries(self):
        self.assertEqual(decision([self.run_at(120, conclusion='failure')], self.now), 'recent-check')

    def test_active_check_is_not_duplicated(self):
        self.assertEqual(decision([self.run_at(500, status='in_progress')], self.now), 'active-check')

    def test_old_checks_and_other_branches_do_not_suppress_monitor(self):
        runs = [self.run_at(900), self.run_at(1000, status='in_progress'), self.run_at(0, branch='test')]
        self.assertEqual(decision(runs, self.now), 'dispatch')

    def test_skipped_run_does_not_count_as_a_health_check(self):
        self.assertEqual(decision([self.run_at(30, conclusion='skipped')], self.now), 'dispatch')

    def test_future_timestamp_reports_configuration_error(self):
        with self.assertRaises(ValueError):
            decision([self.run_at(-120)], self.now)

    def test_read_error_never_triggers_a_blind_duplicate(self):
        request = Mock(side_effect=OSError('not logged'))
        with self.assertRaises(OSError):
            tick('private', request=request, now=lambda: self.now)
        self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
