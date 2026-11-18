"""Deployment observability must be accurate and must not export host secrets."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ops.deployment_diagnostics import DeploymentDiagnostics, extract_diagnostics, public_reason
from ops.deployment_feedback import collect, parse_command, public_report, runtime, self_test
from ops.github_deployment_observer import observe, render_log, ssh_report, target, REPOSITORY
from ops.update_bot import Runner, UpdateError, Updater


OLD, NEW, LATER = 'a' * 40, 'b' * 40, 'c' * 40


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'state_dir': str(self.root), 'repo': str(self.root), 'project': 'reshuffle',
                       'service': 'reshuffle', 'remote': 'origin'}

    def write(self, filename, value):
        path = self.root / filename
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(json.dumps(value))

    def test_only_fixed_diagnostics_and_existing_source_locations_cross_boundary(self):
        (self.root / 'Shuffle.py').write_text('pass')
        raw = ('DISCORD_TOKEN=secret-api-token\nUser message: private meeting notes\n'
               'File "/app/Shuffle.py", line 42\nFile "/app/../../private.py", line 1\n'
               'File "/app/unknown-secret.py", line 9\nModuleNotFoundError: secret.module\n'
               'Dockerfile:12\nprocess "secret-command" did not complete: exit code: 3')
        result = extract_diagnostics(raw, self.root)
        self.assertIn('Source: Shuffle.py:42', result)
        self.assertIn('Dockerfile line 12', result)
        self.assertIn('Build process exit code: 3', result)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('private', json.dumps(result))

    def test_raw_selected_output_is_private_inspect_and_env_are_never_recorded(self):
        log = DeploymentDiagnostics(self.root, self.root)
        log.record(['docker', 'inspect', 'bot'], 0, 'inspect-secret')
        log.record(['docker', 'compose', 'config'], 0, 'env-secret')
        log.record(['docker', 'build', '.'], 1, stderr='build-secret\nNo space left on device')
        public = (log.directory / 'public.json').read_text()
        private = (log.directory / 'private-output.jsonl').read_text()
        self.assertNotIn('secret', public)
        self.assertIn('Disk is full.', public)
        self.assertIn('build-secret', private)
        self.assertNotIn('inspect-secret', private)
        self.assertNotIn('env-secret', private)
        if os.name == 'posix':
            self.assertEqual((log.directory / 'private-output.jsonl').stat().st_mode & 0o777, 0o600)

    def test_failed_and_timed_out_runner_preserves_diagnostics_without_exception_leak(self):
        runner = Runner(1)
        runner.diagnostics = DeploymentDiagnostics(self.root, self.root)
        completed = subprocess.CompletedProcess(['docker', 'build'], 1, '', 'secret\nexit code: 2')
        with patch('ops.update_bot.subprocess.run', return_value=completed), self.assertRaises(UpdateError) as error:
            runner.run(['docker', 'build', '.'])
        self.assertNotIn('secret', str(error.exception))
        with patch('ops.update_bot.subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 1, b'secret', b'out of memory')):
            with self.assertRaises(UpdateError):
                runner.run(['docker', 'build', '.'])
        self.assertTrue(runner.diagnostics.entries[-1]['timeout'])
        self.assertIn('Process ran out of memory.', runner.diagnostics.entries[-1]['diagnostics'])

    def test_full_disk_logging_does_not_interrupt_recovery_commands(self):
        log = DeploymentDiagnostics(self.root, self.root)
        with patch('ops.update_bot.private_json', side_effect=OSError('disk full')):
            log.phase('rollback')
            log.record(['docker', 'start', 'bot'], 0)

    def test_real_runner_build_failure_keeps_bot_running_and_writes_commit_history(self):
        updater = Updater({**self.config, 'command_timeout': 30}, expected_commit=NEW)
        updater.preflight = Mock()
        updater.backup = Mock()
        updater.deploy = Mock()
        def build():
            updater.checkpoint('building')
            updater.runner.run(['docker', 'build', '.'])
        updater.build = build
        completed = subprocess.CompletedProcess([], 1, '', 'private-value\nDockerfile:7\nexit code: 1')
        with patch('ops.update_bot.subprocess.run', return_value=completed):
            report = updater.run()
        updater.backup.assert_not_called()
        updater.deploy.assert_not_called()
        self.assertFalse(updater.stopped)
        saved = json.loads((self.root / 'deployments' / (NEW + '.json')).read_text())
        self.assertEqual(saved['failed_phase'], 'building')
        public = public_report(saved, self.config)
        self.assertNotIn('private-value', json.dumps(public))
        self.assertIn('Dockerfile line 7', json.dumps(public))
        self.assertEqual(report['state'], 'failed')

    def test_reports_do_not_include_raw_error_paths_environment_or_database(self):
        result = public_report({'state': 'failed', 'error': 'secret from host', 'backup': '/private/path',
                                'schema_before': {'user': 'private'}, 'invariant_errors': ['private row']}, self.config)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(public_reason('Command failed: docker'), 'Command failed: docker')
        self.assertEqual(public_reason('Backup verification failed'), 'Backup verification failed')

    def collect(self, *, healthy=True, revision=OLD, runner=None):
        with patch('ops.deployment_feedback.runtime', return_value=({'healthy': healthy, 'revision': revision}, None)):
            return collect(self.config, NEW, runner=runner or Mock())

    def test_failed_revision_remains_failed_after_healthy_rollback(self):
        self.write('deployments/' + NEW + '.json', {'state': 'failed', 'error': 'Command failed: docker',
                                                   'recovery': 'previous-image-healthy-live-data'})
        result = self.collect()
        self.assertEqual(result['state'], 'failure')
        self.assertTrue(result['runtime']['healthy'])
        self.assertEqual(result['deployment']['recovery'], 'previous-image-healthy-live-data')

    def test_success_requires_exact_revision_and_live_readiness(self):
        self.assertEqual(self.collect(revision=NEW)['state'], 'success')
        self.assertEqual(self.collect(revision=NEW, healthy=False)['state'], 'failure')
        self.write('deployments/' + NEW + '.json', {'state': 'deploying'})
        self.assertEqual(self.collect(revision=NEW, healthy=False)['state'], 'pending')

    def test_running_container_with_stale_heartbeat_is_not_healthy(self):
        runner = Mock()
        item = {'Id': 'container', 'State': {'Running': True}, 'Config': {
            'Labels': {'org.opencontainers.image.revision': NEW},
            'Env': ['DISCORD_TOKEN=private-value', 'BOOKCLUB_ENABLED=true']}}
        runner.run.side_effect = ['container', json.dumps([item]), UpdateError('Command failed: docker')]
        result, _ = runtime(self.config, runner)
        self.assertEqual(result, {'healthy': False, 'revision': NEW})
        args = runner.run.call_args.args[0]
        self.assertIn('--require-club', args)
        self.assertIn(NEW, args)
        self.assertNotIn('private-value', json.dumps(result))

    def test_interrupted_build_and_preflight_failures_do_not_stay_pending(self):
        self.write('deployments/' + NEW + '.json', {'state': 'building'})
        self.write('automatic-report.json', {'state': 'blocked_release', 'target_commit': NEW})
        self.assertEqual(self.collect()['state'], 'failure')
        self.write('automatic-report.json', {})
        self.write('poll-status.json', {'state': 'error', 'checked_at': time.time(), 'error': 'Command failed: git'})
        self.assertEqual(self.collect()['state'], 'error')

    def test_unfetched_new_revision_is_pending_not_superseded(self):
        runner = Mock()
        runner.run.side_effect = [OLD, UpdateError('Command failed: git')]
        self.assertEqual(self.collect(runner=runner)['state'], 'pending')
        runner.run.side_effect = [LATER, '']
        self.assertEqual(self.collect(runner=runner)['state'], 'superseded')

    def test_ssh_boundary_rejects_shell_path_and_extra_arguments(self):
        self.assertEqual(parse_command('report ' + NEW), ('report', NEW))
        for command in ('bash', 'report main', 'report ' + NEW + '; id', 'report ' + NEW + '\n',
                        'self-test ../../etc/shadow', 'report ' + NEW + ' --config /etc/shadow'):
            with self.subTest(command=command), self.assertRaises(UpdateError):
                parse_command(command)

    def test_self_test_build_has_no_bot_token_mount_tag_or_container_changes(self):
        result = subprocess.CompletedProcess([], 1, '', 'failed to compute cache key\nDockerfile:2')
        with patch('ops.update_bot.subprocess.run', return_value=result) as run:
            report = self_test(self.config, NEW)
        self.assertEqual(report['state'], 'failure')
        self.assertTrue(report['test'])
        args = run.call_args.args[0]
        self.assertEqual(args[:6], ['docker', 'build', '--network', 'none', '--progress', 'plain'])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('--tag', args)
        self.assertIn('Dockerfile line 2', render_log(report))


class ObserverTests(unittest.TestCase):
    def test_untrusted_pr_or_fork_cannot_observe_or_use_secrets(self):
        run = {'event': 'push', 'head_branch': 'main', 'head_repository': {'full_name': REPOSITORY},
               'path': '.github/workflows/ci.yml', 'head_sha': NEW, 'conclusion': 'success'}
        self.assertEqual(target({'workflow_run': run}, 'workflow_run', OLD), (NEW, 'success'))
        for key, value in [('event', 'pull_request'), ('head_repository', {'full_name': 'attacker/fork'}),
                           ('head_branch', 'feature'), ('path', '.github/workflows/other.yml')]:
            with self.assertRaises(ValueError):
                target({'workflow_run': {**run, key: value}}, 'workflow_run', OLD)

    def test_transient_connection_error_and_startup_are_retried(self):
        fetch = Mock(side_effect=[ValueError('offline'), {'state': 'error', 'commit': NEW},
                                 {'state': 'pending', 'commit': NEW},
                                 {'state': 'success', 'commit': NEW}])
        self.assertEqual(observe(NEW, 'observe', Path('.'), fetch=fetch, sleep=Mock())['state'], 'success')
        self.assertEqual(fetch.call_count, 4)

    def test_timeout_is_error_never_success_and_retains_last_diagnostics(self):
        fetch = Mock(return_value={'state': 'pending', 'commit': NEW, 'deployment': {'failed_phase': 'building'}})
        clock = Mock(side_effect=[0, 0, 31])
        report = observe(NEW, 'observe', Path('.'), fetch=fetch, sleep=Mock(), monotonic=clock, timeout=30)
        self.assertEqual(report['state'], 'error')
        self.assertEqual(report['deployment']['failed_phase'], 'building')

    def test_report_sha_and_test_mode_are_checked(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({'format': 1, 'commit': OLD, 'state': 'success'}), '')
        with patch.dict(os.environ, {'DEPLOY_FEEDBACK_HOST': 'example.org'}), patch(
                'ops.github_deployment_observer.subprocess.run', return_value=response) as run:
            with self.assertRaises(ValueError):
                ssh_report(Path('.'), NEW, 'observe')
        args = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertEqual(args[-1], 'report ' + NEW)


if __name__ == '__main__':
    unittest.main()
