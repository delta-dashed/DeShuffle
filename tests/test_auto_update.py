"""Automatic deployment must fail closed before build/stop on any unverified revision."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops.auto_update import AutomaticUpdater, REPOSITORY, evaluate_runs, workflow_status
from ops.update_bot import UpdateError, Updater


OLD, NEW, LATER = 'a' * 40, 'b' * 40, 'c' * 40


def workflow(**overrides):
    return dict(id=10, run_attempt=1, head_sha=NEW, head_branch='main', event='push',
                head_repository={'full_name': REPOSITORY}, path='.github/workflows/ci.yml',
                status='completed', conclusion='success', html_url='https://github.com/example/run', **overrides)


class CIGateTests(unittest.TestCase):
    def test_only_exact_successful_main_run_can_approve(self):
        self.assertEqual(evaluate_runs([workflow()], NEW)['state'], 'approved')
        for key, value in [('head_sha', OLD), ('head_branch', 'codex/change'), ('event', 'pull_request'),
                           ('head_repository', {'full_name': 'someone/fork'}), ('path', '.github/workflows/other.yml')]:
            item = workflow()
            item[key] = value
            with self.subTest(key=key):
                self.assertEqual(evaluate_runs([item], NEW)['state'], 'waiting_ci')

    def test_latest_failure_or_in_progress_attempt_wins_over_older_success(self):
        recent = {**workflow(), 'id': 11, 'conclusion': 'failure'}
        self.assertEqual(evaluate_runs([workflow(), recent], NEW)['state'], 'blocked_ci')
        retry = {**workflow(), 'run_attempt': 2, 'status': 'in_progress', 'conclusion': None}
        self.assertEqual(evaluate_runs([workflow(), retry], NEW)['state'], 'waiting_ci')
        for outcome in ('cancelled', 'skipped', 'timed_out', 'neutral', 'action_required'):
            self.assertEqual(evaluate_runs([{**workflow(), 'conclusion': outcome}], NEW)['state'], 'blocked_ci')

    def test_network_failure_and_bad_response_never_approve_or_expose_body(self):
        with patch('ops.auto_update.urlopen', side_effect=OSError('private detail')):
            with self.assertRaises(UpdateError) as error:
                workflow_status(NEW)
        self.assertNotIn('private detail', str(error.exception))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        for body in (b'private invalid JSON', b'{}', b'x' * 2_000_001):
            response.read.return_value = body
            with patch('ops.auto_update.urlopen', return_value=response), self.assertRaises(UpdateError):
                workflow_status(NEW)

    def test_api_query_has_exact_revision_and_no_authentication(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'workflow_runs': [workflow()]}).encode()
        with patch('ops.auto_update.urlopen', return_value=response) as request:
            self.assertEqual(workflow_status(NEW)['state'], 'approved')
        args, kwargs = request.call_args
        self.assertIn('head_sha=' + NEW, args[0].full_url)
        self.assertEqual(kwargs['timeout'], 20)
        self.assertIsNone(args[0].get_header('Authorization'))


class AutomaticUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dict(enabled=True, automatic_enabled=True, state_dir=self.tmp.name, remote='origin')
        self.updater = Mock()
        self.updater.old_sha = OLD
        self.updater.current = {'Config': {'Labels': {'org.opencontainers.image.revision': OLD}}}
        self.updater.git.side_effect = lambda *args: NEW if args[0] == 'rev-parse' else ''
        self.updater.run.return_value = {'state': 'updated', 'backup': '/private/backup'}
        self.ci = Mock(return_value={'state': 'approved'})
        self.factory = Mock(return_value=self.updater)
        self.auto = AutomaticUpdater(self.config, updater_factory=self.factory, check_ci=self.ci, now=lambda: 100)

    def test_disabled_does_not_call_git_docker_or_github(self):
        self.config['automatic_enabled'] = False
        self.assertEqual(self.auto.run_once()['state'], 'disabled')
        self.factory.assert_not_called()
        self.ci.assert_not_called()

    def test_unchanged_never_rebuilds_restarts_or_calls_github(self):
        self.updater.old_sha = NEW
        self.updater.current['Config']['Labels']['org.opencontainers.image.revision'] = NEW
        self.assertEqual(self.auto.run_once()['state'], 'unchanged')
        self.updater.run.assert_not_called()
        self.ci.assert_not_called()

    def test_waiting_and_failed_ci_never_invoke_deployment(self):
        for state in ('waiting_ci', 'blocked_ci'):
            self.ci.return_value = {'state': state}
            self.assertEqual(self.auto.run_once()['state'], state)
        self.updater.run.assert_not_called()

    def test_checked_sha_is_pinned_and_journal_precedes_first_side_effect(self):
        def deploy():
            state = json.loads(self.auto.path.read_text())
            self.assertEqual(state['state'], 'deploying')
            self.assertEqual(state['target_commit'], NEW)
            self.assertEqual(self.updater.expected_commit, NEW)
            return {'state': 'updated', 'backup': '/private/backup'}
        self.updater.run.side_effect = deploy
        self.assertEqual(self.auto.run_once()['state'], 'updated')
        self.ci.assert_called_once_with(NEW)

    def test_failed_and_interrupted_release_stays_blocked_until_explicit_retry(self):
        self.updater.run.return_value = {'state': 'failed', 'error': 'Health check failed'}
        self.assertEqual(self.auto.run_once()['state'], 'failed')
        self.assertEqual(self.auto.run_once()['state'], 'blocked_release')
        self.assertEqual(self.auto.run_once()['state'], 'blocked_release')
        self.assertEqual(self.updater.run.call_count, 1)
        self.updater.run.return_value = {'state': 'updated'}
        self.assertEqual(self.auto.run_once(retry_failed=True)['state'], 'updated')
        self.auto.save('deploying', target_commit=NEW)
        self.assertEqual(self.auto.run_once()['state'], 'blocked_release')

    def test_new_commit_can_progress_after_a_failed_previous_commit(self):
        self.auto.save('failed', blocked_commit=OLD, target_commit=OLD)
        self.assertEqual(self.auto.run_once()['state'], 'updated')

    def test_disabling_and_reenabling_preserves_failed_or_interrupted_release_block(self):
        for state in ('failed', 'deploying'):
            self.auto.save(state, target_commit=NEW, **({'blocked_commit': NEW} if state == 'failed' else {}))
            self.config['automatic_enabled'] = False
            self.assertEqual(self.auto.run_once()['state'], 'disabled')
            self.config['automatic_enabled'] = True
            self.assertEqual(self.auto.run_once()['state'], 'blocked_release')
        self.updater.run.assert_not_called()

    def test_unreadable_journal_and_failed_preflight_stop_before_ci_or_deploy(self):
        self.auto.path.write_text('{broken')
        with self.assertRaises(UpdateError):
            self.auto.run_once()
        self.factory.assert_not_called()
        self.auto.path.write_text('{}')
        self.updater.preflight.side_effect = UpdateError('Local changes')
        with self.assertRaises(UpdateError):
            self.auto.run_once()
        self.ci.assert_not_called()
        self.updater.run.assert_not_called()

    def test_remote_change_after_ci_is_rejected_before_build_and_stop(self):
        updater = Updater({'repo': self.tmp.name, 'service': 'reshuffle', 'remote': 'origin',
                           'command_timeout': 30}, expected_commit=NEW)
        updater.git = Mock(side_effect=['', LATER])
        updater.compose = Mock()
        with self.assertRaisesRegex(UpdateError, 'changed after CI'):
            updater.build()
        updater.compose.assert_not_called()


if __name__ == '__main__':
    unittest.main()
