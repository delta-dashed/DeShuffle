"""Exercise the real updater methods against a deterministic Docker/Git boundary."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops.update_bot import (Runner, UpdateError, Updater, compose_literals, load_config,
                            main, parse_compose_json, private_directory)


OLD, NEW = 'a' * 40, 'b' * 40


class FakeHost:
    def __init__(self, config):
        self.config, self.calls = config, []
        self.image_config = {'Env': ['PATH=/usr/local/bin', 'RESHUFFLE_DATA_DIR=/data'],
                             'Cmd': ['python', '-u', 'Shuffle.py'], 'Entrypoint': None,
                             'WorkingDir': '/app', 'User': 'botuser'}
        self.env = {'RESHUFFLE_DATA_DIR': '/data', 'DISCORD_TOKEN': 'private-test-token',
                    'BOOKCLUB_ENABLED': 'true'}
        self.rendered = {'services': {'reshuffle': {'build': {'context': config['repo'], 'dockerfile': 'Dockerfile'},
                                                  'environment': dict(self.env),
                                                  'volumes': [{'type': 'volume', 'source': 'data', 'target': '/data'}]}},
                         'volumes': {'data': {'name': 'reshuffle_data'}}}
        self.current = {'Id': 'old-container', 'Image': 'sha256:old', 'Config': dict(copy.deepcopy(self.image_config),
                        Env=['PATH=/usr/local/bin'] + [key + '=' + value for key, value in self.env.items()],
                        Labels={'com.docker.compose.project': 'reshuffle', 'com.docker.compose.service': 'reshuffle'}),
                        'State': {'Running': True},
                        'Mounts': [{'Type': 'volume', 'Name': 'reshuffle_data', 'Source': config['repo'] + '-volume',
                                    'Destination': '/data', 'RW': True}]}
        self.snapshot = {'format': 1, 'database_exists': True, 'schema': 13, 'active_imports': 0, 'tables': {}}
        self.fail_build = False
        self.stop_response_lost = False
        self.candidate_image = copy.deepcopy(self.image_config)
        self.candidate_transform = lambda value: value
        self.before_up = lambda: None

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'git':
            command = args[3:]
            if command == ['rev-parse', '--show-toplevel']:
                return self.config['repo']
            if command[:2] == ['status', '--porcelain']:
                return ''
            if command == ['branch', '--show-current']:
                return 'main'
            if command[:2] == ['remote', 'get-url']:
                return 'git@github.com:delta-dashed/DeShuffle.git'
            if command == ['rev-parse', 'HEAD']:
                return OLD
            if command == ['rev-parse', 'origin/main']:
                return NEW
            if command[:2] == ['worktree', 'add']:
                Path(command[-2]).mkdir()
            return ''
        if args[:2] == ['docker', 'compose']:
            if 'config' in args:
                value = copy.deepcopy(self.rendered)
                if any('candidate-compose.json' in arg for arg in args):
                    value['services']['reshuffle']['environment']['RESHUFFLE_DEPLOY_SHA'] = NEW
                    value = self.candidate_transform(value)
                return json.dumps(value).replace('$', '$$')
            if 'ps' in args:
                return self.current['Id']
            if 'build' in args and self.fail_build:
                raise UpdateError('Command failed: docker')
            if 'stop' in args:
                self.current['State']['Running'] = False
                if self.stop_response_lost:
                    raise UpdateError('Command failed: docker')
            if 'up' in args:
                self.before_up()
                container = copy.deepcopy(self.current)
                candidate = any('candidate-compose.json' in arg for arg in args)
                container['Id'] = 'new-container' if candidate else 'recovered-container'
                container['Image'] = 'sha256:new' if candidate else 'sha256:old'
                container['State']['Running'] = True
                if candidate:
                    container['Config']['Env'].append('RESHUFFLE_DEPLOY_SHA=' + NEW)
                    container['Config']['Labels']['org.opencontainers.image.revision'] = NEW
                self.current = container
            return ''
        if args[:2] == ['docker', 'inspect']:
            return json.dumps([self.current])
        if args[:3] == ['docker', 'image', 'inspect']:
            return json.dumps([{'Config': self.image_config if args[-1] == 'sha256:old' else self.candidate_image}])
        if args[:2] == ['docker', 'ps']:
            return self.current['Id'] if self.current['State']['Running'] else ''
        if args[:2] == ['docker', 'start']:
            self.current['State']['Running'] = True
            return self.current['Id']
        if args[:2] == ['docker', 'exec']:
            return 'no' if 'shutil.which' in args[-1] else 'healthy'
        if args[:2] == ['docker', 'run']:
            if 'capture' in args:
                return json.dumps(self.snapshot)
            if 'shutil.which' in args[-1]:
                return 'no'
            return json.dumps({'size': 100, 'sha256': 'c' * 64})
        raise AssertionError('Unexpected boundary call: ' + args[0])


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        repo = base / 'repo'
        repo.mkdir()
        (repo / '.env').write_text('DISCORD_TOKEN=private-test-token\n', encoding='utf-8')
        (repo / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
        self.path = base / 'config.json'
        self.raw = {'enabled': True, 'repo': str(repo), 'backup_dir': str(base / 'backups'),
                    'state_dir': str(base / 'state')}
        self.path.write_text(json.dumps(self.raw), encoding='utf-8')
        self.config = load_config(self.path)
        Path(self.config['state_dir']).mkdir()
        self.host = FakeHost(self.config)
        self.updater = Updater(self.config, self.host, now=lambda: 1000000000.0)

    def test_preflight_only_reads_and_never_calls_model(self):
        self.updater.preflight()
        args = self.host.calls
        self.assertFalse(any('stop' in command or 'build' in command or 'fetch' in command for command in args))
        self.assertFalse(any(command[:2] == ['docker', 'start'] for command in args))
        self.assertEqual(list(Path(self.config['state_dir']).iterdir()), [])
        helper = next(command for command in args if 'capture' in command)
        self.assertIn('none', helper)
        self.assertIn('old-container:ro', helper)
        self.assertNotIn('codex', [part for command in args for part in command])

    def test_wrong_actual_volume_fails_before_stop(self):
        self.host.current['Mounts'][0]['Name'] = 'unrelated-data'
        with self.assertRaisesRegex(UpdateError, 'named volume'):
            self.updater.preflight()

    def test_changed_env_file_does_not_silently_switch_discord_account(self):
        self.host.rendered['services']['reshuffle']['environment']['DISCORD_TOKEN'] = 'different-secret'
        with self.assertRaisesRegex(UpdateError, 'environment differs'):
            self.updater.preflight()

    def test_removed_runtime_import_setting_fails_preflight(self):
        self.host.current['Config']['Env'].append('BOOKCLUB_IMPORT_CONFIG_FILE=/data/import.json')
        with self.assertRaisesRegex(UpdateError, 'environment differs'):
            self.updater.preflight()

    def test_literal_dollars_preserved_between_compose_and_actual_container(self):
        literal = 'test$VAR${TOKEN}$$literal'
        self.host.rendered['services']['reshuffle']['environment']['BOOKCLUB_CONFIG_FILE'] = literal
        self.host.current['Config']['Env'].append('BOOKCLUB_CONFIG_FILE=' + literal)
        self.updater.preflight()
        self.updater.build()
        self.assertEqual(self.updater.frozen_env['BOOKCLUB_CONFIG_FILE'], literal)
        self.updater.backup()
        saved = json.loads(self.updater.rollback_base.read_text(encoding='utf-8'))
        self.assertEqual(saved['services']['reshuffle']['environment']['BOOKCLUB_CONFIG_FILE'], literal.replace('$', '$$'))

    def test_backup_path_inside_data_is_rejected(self):
        self.host.current['Mounts'][0]['Source'] = str(Path(self.temp.name))
        with self.assertRaisesRegex(UpdateError, 'overlap persistent mounts'):
            self.updater.preflight()

    def test_active_import_cannot_be_interrupted(self):
        self.host.snapshot['active_imports'] = 1
        with self.assertRaisesRegex(UpdateError, 'import is running'):
            self.updater.preflight()

    def test_build_error_leaves_current_bot_running(self):
        self.host.fail_build = True
        report = self.updater.run()
        self.assertEqual(report['state'], 'failed')
        self.assertTrue(self.host.current['State']['Running'])
        self.assertFalse(any('stop' in command for command in self.host.calls))
        self.assertNotIn('private-test-token', json.dumps(report))

    def test_complete_manual_update_uses_backup_before_up_and_advances_main_last(self):
        def require_backup():
            backups = list(Path(self.config['backup_dir']).glob('*/manifest.json'))
            self.assertEqual(len(backups), 1)
        self.host.before_up = require_backup
        report = self.updater.run()
        self.assertEqual(report['state'], 'updated')
        self.assertEqual(report['schema_before'], 13)
        self.assertEqual(report['schema_after'], 13)
        self.assertTrue(self.host.current['State']['Running'])
        calls = self.host.calls
        build_index = next(i for i, args in enumerate(calls) if 'build' in args)
        stop_index = next(i for i, args in enumerate(calls) if 'stop' in args)
        up_index = next(i for i, args in enumerate(calls) if 'up' in args)
        merge_index = next(i for i, args in enumerate(calls) if args[-3:] == ['merge', '--ff-only', NEW])
        self.assertLess(build_index, stop_index)
        self.assertLess(stop_index, up_index)
        self.assertLess(up_index, merge_index)
        self.assertNotIn('private-test-token', json.dumps(report))

    def test_failed_health_rolls_back_with_frozen_config_and_keeps_live_volume(self):
        def unhealthy(*_args, **_kwargs):
            # Simulate an unrelated edit while a candidate is running. Recovery
            # must not apply these newly edited mounts or environment variables.
            Path(self.config['compose_files'][0]).write_text('unrelated changes', encoding='utf-8')
            raise UpdateError('New runtime did not pass checks')
        self.updater.wait_healthy = unhealthy
        report = self.updater.run()
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['recovery'], 'previous-image-running-manual-health-check')
        recovery_up = [args for args in self.host.calls if 'up' in args][-1]
        self.assertIn(str(Path(self.config['state_dir']) / 'previous-compose.json'), recovery_up)
        self.assertNotIn(self.config['compose_files'][0], recovery_up)
        self.assertNotIn('--env-file', recovery_up)
        self.assertFalse(any('extractall' in part or 'restore' == part for args in self.host.calls for part in args))

    def test_named_volume_redefinition_is_rejected_before_build(self):
        self.updater.preflight()
        def change(value):
            value['volumes']['data']['name'] = 'empty-other-volume'
            return value
        self.host.candidate_transform = change
        with self.assertRaisesRegex(UpdateError, 'named volume'):
            self.updater.build()
        self.assertFalse(any('build' in command for command in self.host.calls))

    def test_candidate_image_cannot_enable_importer_implicitly(self):
        self.updater.preflight()
        self.host.candidate_image['Env'].append('BOOKCLUB_IMPORT_CONFIG_FILE=/data/unexpected.json')
        with self.assertRaisesRegex(UpdateError, 'application configuration'):
            self.updater.build()

    def test_candidate_image_cannot_change_default_command(self):
        self.updater.preflight()
        self.host.candidate_image['Cmd'] = ['sleep', 'infinity']
        with self.assertRaisesRegex(UpdateError, 'command/user'):
            self.updater.build()

    def test_stop_response_lost_restarts_exact_original_container(self):
        self.host.stop_response_lost = True
        report = self.updater.run()
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['recovery'], 'original-container-restarted')
        self.assertTrue(self.host.current['State']['Running'])
        self.assertIn(['docker', 'start', 'old-container'], self.host.calls)
        self.assertFalse(any('up' in command for command in self.host.calls))

    def test_configuration_change_during_backup_never_recreates_original(self):
        self.updater.preflight()
        self.updater.build()
        self.updater.backup()
        Path(self.config['compose_files'][0]).write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(UpdateError, 'configuration changed'):
            self.updater.deploy()
        self.updater.rollback()
        self.assertIn(['docker', 'start', 'old-container'], self.host.calls)
        self.assertFalse(any('up' in command for command in self.host.calls))

    def test_backup_manifest_contains_no_env_values_and_preserves_private_files(self):
        self.updater.preflight()
        self.updater.build()
        self.updater.backup()
        directory = Path(self.updater.report['backup'])
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['schema'], 13)
        self.assertNotIn('private-test-token', json.dumps(manifest))
        saved_env = next(item for item in manifest['host_files'] if item['source'].endswith('.env'))
        self.assertIn('private-test-token', (directory / saved_env['backup']).read_text(encoding='utf-8'))
        backup_call = next(command for command in self.host.calls if command[:2] == ['docker', 'run'] and 'tarfile' in command[-1])
        self.assertIn('old-container:ro', backup_call)
        self.assertNotIn('[f.read()', backup_call[-1])

    def test_changed_checkout_branch_is_refused(self):
        real = self.host.run
        self.host.run = lambda args, **kw: 'feature/x' if args[-2:] == ['branch', '--show-current'] else real(args, **kw)
        with self.assertRaisesRegex(UpdateError, 'must be on main'):
            self.updater.preflight()

    def test_compose_keeps_project_override_order_and_env_file(self):
        override = Path(self.temp.name) / 'server.override.yml'
        self.config['compose_files'].append(str(override))
        self.updater.compose('ps', '--quiet', 'reshuffle')
        command = self.host.calls[-1]
        self.assertEqual(command[command.index('--project-name') + 1], 'reshuffle')
        self.assertLess(command.index(self.config['compose_files'][0]), command.index(str(override)))
        self.assertIn(self.config['env_file'], command)

    def test_config_rejects_unsafe_directories_and_non_main(self):
        for changes in ({'backup_dir': self.raw['repo']}, {'state_dir': self.temp.name},
                        {'backup_dir': self.raw['state_dir']}, {'branch': 'feature/x'}, {'database_path': '/data/../elsewhere'}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps(dict(self.raw, **changes)), encoding='utf-8')
                with self.assertRaises(UpdateError):
                    load_config(self.path)

    def test_disabled_config_never_runs_a_command(self):
        self.path.write_text(json.dumps(dict(self.raw, enabled=False)), encoding='utf-8')
        with patch.object(Runner, 'run', side_effect=AssertionError('must not run')):
            self.assertEqual(main(['--config', str(self.path)]), 1)

    def test_subprocess_failure_and_timeout_do_not_leak_stderr_or_arguments(self):
        runner = Runner(20)
        for effect in (subprocess.CompletedProcess(['docker'], 1, 'sensitive stdout', 'private-test-token'),
                       subprocess.TimeoutExpired('private-test-token', 20, output='private-test-token')):
            with self.subTest(effect=type(effect).__name__):
                with patch('subprocess.run', side_effect=effect if isinstance(effect, Exception) else None,
                           return_value=effect):
                    with self.assertRaises(UpdateError) as caught:
                        runner.run(['docker', 'secret=value'])
                    self.assertNotIn('private-test-token', str(caught.exception))
                    self.assertNotIn('secret=value', str(caught.exception))

    def test_compose_literals_escape_values_recursively(self):
        value = {'environment': {'SECRET': 'a${TOKEN}$b'}, 'command': ['echo', '$literal']}
        self.assertEqual(compose_literals(value)['environment']['SECRET'], 'a$${TOKEN}$$b')
        self.assertEqual(value['command'][1], '$literal')

    def test_compose_serialization_decodes_once_including_mapping_keys(self):
        original = {'labels': {'literal$key': 'a$${VAR}$$$b'}, 'command': ['echo', '$$literal']}
        serialized = json.dumps(original).replace('$', '$$')
        self.assertEqual(parse_compose_json(serialized), original)

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory permissions')
    def test_shared_directory_permissions_are_never_changed(self):
        folder = Path(self.temp.name) / 'shared'
        folder.mkdir(mode=0o755)
        with self.assertRaises(UpdateError):
            private_directory(folder)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()
