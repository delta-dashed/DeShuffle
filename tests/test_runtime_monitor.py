"""Late failures must be visible without changing deployments or exposing bot data."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops.deployment_feedback import parse_command
from ops.github_deployment_observer import ssh_report
from ops.github_runtime_monitor import main, monitor
from ops.runtime_monitor import health_report, planned_restart, probe, updater_locked
from ops.update_bot import UpdateError


OLD, NEW = 'a' * 40, 'b' * 40


def response(state='healthy', **values):
    return {'format': 1, 'kind': 'runtime-health', 'commit': NEW, 'observed_at': 1000, 'state': state,
            'runtime': {'healthy': state == 'healthy', 'revision': OLD, 'reasons': []}, **values}


class ServerHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'repo': str(Path(__file__).resolve().parents[1]), 'state_dir': self.tmp.name,
                       'service': 'reshuffle', 'project': 'reshuffle', 'health_timeout': 300}
        self.item = {'Id': 'bot', 'State': {'Running': True}, 'Config': {
            'Labels': {'org.opencontainers.image.revision': OLD},
            'Env': ['DISCORD_TOKEN=secret-token', 'BOOKCLUB_ENABLED=true']}}

    def runner(self, result):
        return Mock(run=Mock(side_effect=['bot', json.dumps([self.item]), json.dumps(result)]))

    def test_healthy_old_revision_is_healthy_even_after_new_release_failed(self):
        (self.root / 'last-report.json').write_text(json.dumps({'state': 'failed', 'target_commit': NEW}))
        runner = self.runner({'healthy': True, 'reasons': []})
        result = health_report(self.config, NEW, runner=runner, now=lambda: 1000)
        self.assertEqual(result['state'], 'healthy')
        self.assertEqual(result['runtime']['revision'], OLD)
        self.assertNotIn('secret-token', json.dumps(result))
        args = runner.run.call_args.args[0]
        self.assertIn('--require-club', args)
        self.assertEqual(args[args.index('--expected-sha') + 1], OLD)

    def test_late_stale_heartbeat_discord_or_club_errors_are_unhealthy(self):
        for reason in ('stale-heartbeat', 'discord-disconnected', 'reconciliation-failed', 'stale-reconciliation'):
            with self.subTest(reason=reason):
                result = health_report(self.config, NEW, runner=self.runner({'healthy': False, 'reasons': [reason]}),
                                       now=lambda: 1000)
                self.assertEqual(result['state'], 'unhealthy')
                self.assertEqual(result['runtime']['reasons'], [reason])

    def test_stopped_missing_or_duplicate_container_is_detected(self):
        self.item['State']['Running'] = False
        result = probe(self.config, self.runner({}))
        self.assertEqual(result['reasons'], ['container-stopped-or-restarting'])
        for ids in ('', 'first\nsecond'):
            result = probe(self.config, Mock(run=Mock(return_value=ids)))
            self.assertEqual(result['reasons'], ['container-missing-or-duplicate'])

    def test_docker_errors_and_arbitrary_messages_are_not_exported(self):
        runner = Mock(run=Mock(side_effect=UpdateError('private-token-and-user-message')))
        result = health_report(self.config, NEW, runner=runner)
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('private', json.dumps(result))
        result = health_report(self.config, NEW, runner=self.runner({'healthy': False, 'reasons': ['secret-token']}))
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('secret-token', json.dumps(result))

    def test_restart_grace_requires_lock_recent_phase_and_is_bounded(self):
        path = self.root / 'last-report.json'
        path.write_text(json.dumps({'state': 'deploying', 'started_at': 1, 'phase_started_at': 900}))
        with patch('ops.runtime_monitor.updater_locked', return_value=True):
            self.assertTrue(planned_restart(self.config, 1000))
            self.assertFalse(planned_restart(self.config, 1400))
            self.assertFalse(planned_restart(self.config, 899))
        with patch('ops.runtime_monitor.updater_locked', return_value=False):
            self.assertFalse(planned_restart(self.config, 1000))
        path.write_text(json.dumps({'state': 'building', 'phase_started_at': 999}))
        with patch('ops.runtime_monitor.updater_locked', return_value=True):
            self.assertFalse(planned_restart(self.config, 1000))

    @unittest.skipUnless(os.name == 'posix', 'Linux deployment lock')
    def test_actual_deployment_lock_is_checked_without_modifying_it(self):
        import fcntl
        path = self.root / 'update.lock'
        path.write_text('')
        self.assertFalse(updater_locked(self.root))
        with path.open('r') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(updater_locked(self.root))
        self.assertFalse(updater_locked(self.root))

    def test_stale_heartbeat_self_test_uses_real_checker_without_docker_or_live_file(self):
        runner = Mock()
        result = health_report(self.config, NEW, runner=runner, now=lambda: 5000, test=True)
        self.assertEqual(result['state'], 'unhealthy')
        self.assertEqual(result['runtime']['reasons'], ['stale-heartbeat'])
        self.assertTrue(result['test'])
        runner.run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_health_commands_cannot_open_shell_or_read_arbitrary_paths(self):
        self.assertEqual(parse_command('health ' + NEW), ('health', NEW))
        self.assertEqual(parse_command('health-self-test ' + NEW), ('health-self-test', NEW))
        for command in ('health main', 'health ' + NEW + '; id', 'health ' + NEW + ' /etc/shadow'):
            with self.assertRaises(UpdateError):
                parse_command(command)


class ExternalMonitorTests(unittest.TestCase):
    def run_monitor(self, fetch, **kwargs):
        return monitor(NEW, Path('.'), fetch=fetch, sleep=Mock(), now=lambda: 1000, **kwargs)

    def test_transient_network_failure_and_recovery_does_not_raise_incident(self):
        fetch = Mock(side_effect=[OSError('private hostname'), response()])
        self.assertEqual(self.run_monitor(fetch)['state'], 'healthy')
        self.assertEqual(fetch.call_count, 2)

    def test_three_failed_probes_report_unhealthy_or_unreachable(self):
        for side_effect, expected in [(OSError('secret'), 'unreachable'), (None, 'unhealthy')]:
            fetch = Mock(side_effect=side_effect, return_value=response('unhealthy'))
            result = self.run_monitor(fetch)
            self.assertEqual(result['state'], expected)
            self.assertEqual(fetch.call_count, 3)
            self.assertNotIn('secret', json.dumps(result))

    def test_planned_restart_is_waited_out_without_false_success(self):
        fetch = Mock(side_effect=[response('maintenance'), response('maintenance'), response()])
        self.assertEqual(self.run_monitor(fetch)['state'], 'healthy')
        fetch = Mock(return_value=response('maintenance'))
        result = self.run_monitor(fetch, monotonic=Mock(side_effect=[0, 0, 421]))
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['reasons'], ['readiness-not-confirmed-before-monitor-deadline'])

    def test_stale_or_contradictory_response_is_never_green(self):
        for item in (response(observed_at=1), response(runtime={'healthy': False})):
            self.assertEqual(self.run_monitor(Mock(return_value=item))['state'], 'unreachable')

    def test_unreachable_test_only_connects_to_closed_local_port(self):
        with patch.dict(os.environ, {'DEPLOY_FEEDBACK_HOST': 'production.example'}), patch(
                'ops.github_deployment_observer.subprocess.run', return_value=subprocess.CompletedProcess([], 255, '', 'private')) as run:
            with self.assertRaises(ValueError):
                ssh_report(Path('.'), NEW, 'health', unreachable_test=True)
        args = run.call_args.args[0]
        self.assertIn('root@127.0.0.1', args)
        self.assertEqual(args[args.index('-p') + 1], '1')
        self.assertNotIn('production.example', args)
        self.assertIn('StrictHostKeyChecking=yes', args)

    def test_workflow_writes_failure_report_and_nonzero_exit_without_publishing_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = Path.cwd()
            os.chdir(root)
            try:
                env = {'GITHUB_SHA': NEW, 'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'schedule',
                       'GITHUB_STEP_SUMMARY': str(root / 'summary.md'), 'DEPLOY_FEEDBACK_KEY': 'secret-key',
                       'DEPLOY_FEEDBACK_KNOWN_HOSTS': 'secret-host-key'}
                report = {'format': 1, 'commit': NEW, 'mode': 'check', 'checked_at': 1000,
                          'state': 'unreachable', 'reasons': ['server-or-restricted-ssh-endpoint-unreachable'], 'samples': []}
                with patch.dict(os.environ, env), patch('ops.github_runtime_monitor.monitor', return_value=report):
                    self.assertEqual(main([]), 1)
                self.assertEqual(json.loads((root / 'runtime-health-report/report.json').read_text())['state'], 'unreachable')
                self.assertNotIn('secret', (root / 'summary.md').read_text())
            finally:
                os.chdir(before)


if __name__ == '__main__':
    unittest.main()
