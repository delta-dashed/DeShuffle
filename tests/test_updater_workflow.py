"""Host updater orchestration and recovery with no Docker or network access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ops.update_bot import UpdateError, Updater


OLD = 'a' * 40
NEW = 'b' * 40


def config_for(directory):
    root = Path(directory)
    for name in ('repo', 'state', 'backups'):
        (root / name).mkdir(exist_ok=True)
    return {'repo': str(root / 'repo'), 'state_dir': str(root / 'state'),
            'backup_dir': str(root / 'backups'), 'command_timeout': 30,
            'health_timeout': 15, 'service': 'reshuffle'}


class WorkflowUpdater(Updater):
    """Replace external effects, retaining the real run/error/report boundary."""

    def __init__(self, config, fail_at=None, unchanged=False):
        super().__init__(config, now=lambda: 1000)
        self.events = []
        self.fail_at = fail_at
        self.unchanged = unchanged

    def step(self, event):
        self.events.append(event)
        if self.fail_at == event:
            raise UpdateError('Injected ' + event + ' failure')

    def preflight(self):
        self.step('preflight')
        self.old_sha = OLD
        self.current = {'Id': 'old', 'Image': 'old-image'}

    def build(self):
        self.step('build')
        self.target = NEW
        if self.unchanged:
            self.report['state'] = 'unchanged'
        else:
            self.candidate = Path(self.config['state_dir']) / 'candidate'

    def backup(self):
        self.step('backup-preflight')
        self.stopped = True
        self.step('stop')
        self.step('backup-verified')

    def deploy(self):
        self.deployed = True
        self.step('candidate-start')
        self.step('health')
        self.step('invariants')
        self.step('checkout-advance')
        self.stopped = False
        self.report['state'] = 'updated'

    def rollback(self):
        if not self.stopped:
            return
        self.step('rollback')
        self.report['recovery'] = 'previous-image-healthy-live-data'
        self.stopped = False

    def git(self, *args):
        if args[:2] == ('worktree', 'remove'):
            self.step('cleanup')
            return ''
        raise AssertionError('Unexpected git mutation')


class UpdaterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config_for(self.temporary.name)

    def test_success_only_deploys_after_verified_backup_and_checks(self):
        updater = WorkflowUpdater(self.config)
        report = updater.run()
        self.assertEqual(report['state'], 'updated')
        self.assertEqual(updater.events, ['preflight', 'build', 'backup-preflight', 'stop',
                                          'backup-verified', 'candidate-start', 'health', 'invariants',
                                          'checkout-advance', 'cleanup'])
        self.assertFalse(updater.stopped)
        self.assertEqual(json.loads((Path(self.config['state_dir']) / 'last-report.json').read_text()), report)

    def test_unchanged_release_never_stops_backs_up_or_redeploys(self):
        updater = WorkflowUpdater(self.config, unchanged=True)
        self.assertEqual(updater.run()['state'], 'unchanged')
        self.assertEqual(updater.events, ['preflight', 'build'])

    def test_preflight_and_build_errors_leave_running_bot_untouched(self):
        for failure in ('preflight', 'build', 'backup-preflight'):
            with self.subTest(failure=failure):
                updater = WorkflowUpdater(self.config, fail_at=failure)
                report = updater.run()
                self.assertEqual(report['state'], 'failed')
                self.assertNotIn('stop', updater.events)
                self.assertNotIn('candidate-start', updater.events)
                self.assertNotIn('rollback', updater.events)

    def test_stop_and_backup_errors_restart_previous_image_without_deploy(self):
        for failure in ('stop', 'backup-verified'):
            with self.subTest(failure=failure):
                updater = WorkflowUpdater(self.config, fail_at=failure)
                report = updater.run()
                self.assertEqual(report['state'], 'failed')
                self.assertIn('rollback', updater.events)
                self.assertNotIn('candidate-start', updater.events)
                self.assertEqual(report['recovery'], 'previous-image-healthy-live-data')

    def test_deploy_health_invariant_and_checkout_failures_invoke_recovery(self):
        for failure in ('candidate-start', 'health', 'invariants', 'checkout-advance'):
            with self.subTest(failure=failure):
                updater = WorkflowUpdater(self.config, fail_at=failure)
                report = updater.run()
                self.assertEqual(report['state'], 'failed')
                self.assertEqual(updater.events.count('rollback'), 1)
                self.assertEqual(updater.events[-2:], ['rollback', 'cleanup'])
                self.assertEqual(report['recovery'], 'previous-image-healthy-live-data')

    def test_rollback_failure_is_reported_and_candidate_cleanup_still_runs(self):
        updater = WorkflowUpdater(self.config, fail_at='rollback')
        original_deploy = updater.deploy

        def failed_deploy():
            original_deploy()
            updater.stopped = True
            raise UpdateError('Deployment rejected')

        updater.deploy = failed_deploy
        report = updater.run()
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['recovery'], 'manual-recovery-required')
        self.assertEqual(updater.events[-1], 'cleanup')
        self.assertIn('rollback', report['recovery_error'])

    def test_cleanup_failure_does_not_misreport_successful_running_release(self):
        updater = WorkflowUpdater(self.config, fail_at='cleanup')
        report = updater.run()
        self.assertEqual(report['state'], 'updated')
        self.assertEqual(report['worktree_cleanup'], 'manual-cleanup-required')


class RollbackUpdater(Updater):
    """Exercise the real recovery method against a recorded Docker boundary."""

    def __init__(self, config, after_schema=13, candidate_stops=True, versioned=True):
        runner = Mock()
        runner.run.side_effect = AssertionError('Unexpected direct command during recovery')
        super().__init__(config, runner=runner, now=lambda: 1000)
        self.events = []
        self.container_reads = []
        self.stopped = self.deployed = True
        self.before = {'schema': 13}
        self.current = {'Id': 'old', 'Image': 'sha256:old-image', 'Config': {
            'Env': ['RESHUFFLE_DEPLOY_SHA=' + OLD] if versioned else []}}
        self.frozen_env = {}
        self.active = {'Id': 'candidate', 'Image': 'sha256:candidate'}
        self.after_schema = after_schema
        self.candidate_stops = candidate_stops
        self.override = Path(config['state_dir']) / 'candidate-compose.json'

    def compose(self, *args, **kwargs):
        self.events.append(('compose', args, kwargs))
        if args[0] == 'up':
            self.active = self.current
        return ''

    def container(self, *, frozen=False):
        self.container_reads.append(frozen)
        return self.active

    def inspect(self, identifier):
        return {'State': {'Running': identifier == 'old' or not self.candidate_stops}}

    def snapshot(self, container):
        self.events.append(('snapshot', container['Id']))
        return {'schema': self.after_schema}

    def no_duplicate_bot(self, container):
        self.events.append(('no-duplicate', container['Id']))

    def wait_healthy(self, container, sha, *, started_after):
        self.events.append(('health', container['Id'], sha, started_after))


class UpdaterRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config_for(self.temporary.name)

    def test_same_schema_restarts_old_image_on_live_data_without_db_restore(self):
        updater = RollbackUpdater(self.config)
        updater.rollback()
        self.assertEqual(updater.report['recovery'], 'previous-image-healthy-live-data')
        commands = [event for event in updater.events if event[0] == 'compose']
        self.assertEqual([event[1][0] for event in commands], ['stop', 'up'])
        self.assertTrue(commands[0][2]['frozen'])
        self.assertTrue(commands[1][2]['frozen'])
        self.assertEqual(updater.container_reads, [True, True])
        self.assertEqual(updater.events[1], ('snapshot', 'candidate'))
        self.assertIn(('no-duplicate', 'old'), updater.events)
        self.assertTrue(any(event[:3] == ('health', 'old', OLD) for event in updater.events))
        override = json.loads((Path(self.config['state_dir']) / 'rollback-compose.json').read_text())
        service = override['services']['reshuffle']
        self.assertEqual(service['image'], 'sha256:old-image')
        self.assertNotIn('volumes', service)
        self.assertEqual(set(service), {'image', 'environment', 'labels'})
        self.assertFalse(updater.stopped)

    def test_schema_change_stops_candidate_and_requires_manual_recovery(self):
        updater = RollbackUpdater(self.config, after_schema=14)
        updater.rollback()
        self.assertEqual(updater.report['recovery'], 'manual-schema-change')
        self.assertTrue(updater.stopped)
        self.assertFalse(any(event[0] == 'compose' and event[1][0] == 'up' for event in updater.events))
        self.assertFalse((Path(self.config['state_dir']) / 'rollback-compose.json').exists())

    def test_failure_to_stop_candidate_cannot_start_a_second_bot(self):
        updater = RollbackUpdater(self.config, candidate_stops=False)
        with self.assertRaisesRegex(UpdateError, 'refusing second bot'):
            updater.rollback()
        self.assertEqual(len(updater.events), 1)

    def test_lost_stop_response_is_checked_before_old_image_restart(self):
        updater = RollbackUpdater(self.config)
        compose = updater.compose

        def lost_stop(*args, **kwargs):
            result = compose(*args, **kwargs)
            if args[0] == 'stop':
                raise UpdateError('Stop response lost')
            return result

        updater.compose = lost_stop
        updater.rollback()
        self.assertEqual(updater.report['recovery'], 'previous-image-healthy-live-data')
        self.assertEqual([event[1][0] for event in updater.events if event[0] == 'compose'], ['stop', 'up'])

    def test_backup_failure_restarts_exact_original_container_without_composing_config(self):
        updater = RollbackUpdater(self.config, versioned=False)
        updater.deployed = False
        updater.runner.run.side_effect = None
        updater.rollback()
        updater.runner.run.assert_called_once_with(['docker', 'start', 'old'])
        self.assertEqual(updater.report['recovery'], 'original-container-restarted')
        self.assertFalse(updater.stopped)
        self.assertEqual(updater.events, [('no-duplicate', 'old')])

    def test_external_duplicate_blocks_restart_before_any_container_command(self):
        updater = RollbackUpdater(self.config)
        updater.deployed = False
        updater.no_duplicate_bot = Mock(side_effect=UpdateError('Another bot instance is running'))
        with self.assertRaisesRegex(UpdateError, 'Another bot'):
            updater.rollback()
        updater.runner.run.assert_not_called()
        self.assertTrue(updater.stopped)
        self.assertEqual(updater.events, [])

    def test_old_unversioned_image_uses_manual_health_fallback_after_candidate_deploy(self):
        updater = RollbackUpdater(self.config, versioned=False)
        updater.rollback()
        self.assertEqual(updater.report['recovery'], 'previous-image-running-manual-health-check')
        self.assertFalse(any(event[0] == 'health' for event in updater.events))


class UpdaterHealthPollingTests(unittest.TestCase):
    def test_runtime_checker_is_retried_and_receives_release_and_process_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000]
            runner = Mock()
            runner.run.side_effect = [UpdateError('stale heartbeat'), UpdateError('wrong release'), 'healthy']
            updater = Updater(config_for(directory), runner=runner, now=lambda: clock[0],
                              sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            updater.require_club = True
            updater.health_file = '/data/runtime-health.json'
            updater.inspect = Mock(return_value={'State': {'Running': True}})
            updater.wait_healthy({'Id': 'new'}, NEW, started_after=999)
            self.assertEqual(runner.run.call_count, 3)
            for call in runner.run.call_args_list:
                command = call.args[0]
                self.assertIn('--require-club', command)
                self.assertEqual(command[command.index('--expected-sha') + 1], NEW)
                self.assertEqual(command[command.index('--started-after') + 1], '999')
                self.assertEqual(command[command.index('--check') + 1], '/data/runtime-health.json')

    def test_stopped_or_restarting_candidate_never_passes_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000]
            runner = Mock()
            updater = Updater(config_for(directory), runner=runner, now=lambda: clock[0],
                              sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
            updater.inspect = Mock(side_effect=[{'State': {'Running': False}},
                                               {'State': {'Running': True, 'Restarting': True}},
                                               {'State': {'Running': False}}])
            with self.assertRaisesRegex(UpdateError, 'health checks in time'):
                updater.wait_healthy({'Id': 'new'}, NEW, started_after=999)
            runner.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
