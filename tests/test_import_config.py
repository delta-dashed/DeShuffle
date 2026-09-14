"""Host-only import settings are strict immutable startup snapshots."""
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookclub.import_config import ImportConfig, load_import_config
from bookclub.store import ClubError


class ImportConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bookclub.import.json'

    def load(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        return load_import_config(self.path)

    def test_absent_path_is_disabled_with_one_run(self):
        with patch.dict(os.environ, {'BOOKCLUB_IMPORT_CONFIG_FILE': ''}):
            config = load_import_config()
        self.assertEqual(config, ImportConfig())
        self.assertFalse(config.enabled)
        self.assertEqual(config.max_runs, 1)

    def test_example_stays_disabled(self):
        example = Path(__file__).resolve().parents[1] / 'bookclub.import.example.json'
        config = load_import_config(example)
        self.assertFalse(config.enabled)
        self.assertEqual(config.max_runs, 1)

    def test_enabled_requires_each_allowlist(self):
        valid = dict(enabled=True, allowed_user_ids=[1], allowed_guild_ids=[2], allowed_channel_ids=[3])
        self.assertTrue(self.load(valid).enabled)
        for key in ('allowed_user_ids', 'allowed_guild_ids', 'allowed_channel_ids'):
            with self.subTest(key=key), self.assertRaises(ClubError):
                self.load({**valid, key: []})

    def test_unknown_fields_and_nonobject_are_rejected(self):
        for value in ({'enable': True}, {'api_key': 'must-not-be-used'}, [], None, 'string', 1):
            with self.subTest(value=value), self.assertRaises(ClubError):
                self.load(value)

    def test_enabled_does_not_coerce_strings_or_integers(self):
        for value in ('true', 'false', 1, 0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ClubError):
                self.load({'enabled': value})

    def test_ids_are_strict_positive_bounded_integers(self):
        for key in ('allowed_user_ids', 'allowed_guild_ids', 'allowed_channel_ids'):
            for value in (['1'], [True], [False], [0], [-1], [2**63], [1.0], None, '1', 1, {}):
                with self.subTest(key=key, value=value), self.assertRaises(ClubError):
                    self.load({key: value})
            self.assertEqual(getattr(self.load({key: [1, 2**63 - 1, 1]}), key), (1, 2**63 - 1))

    def test_limits_reject_wrong_types_and_out_of_range_values(self):
        bounds = dict(max_runs=(1, 100), max_accounted_tokens=(1, 10_000_000),
                      max_messages=(1, 1000), max_threads=(1, 100),
                      max_input_bytes=(2000, 200_000), max_attachment_bytes=(0, 100_000_000),
                      timeout_seconds=(30, 600))
        for key, (low, high) in bounds.items():
            for value in (True, False, str(low), float(low), None, low - 1, high + 1):
                with self.subTest(key=key, value=value), self.assertRaises(ClubError):
                    self.load({key: value})
            for value in (low, high):
                with self.subTest(key=key, bound=value):
                    self.assertEqual(getattr(self.load({key: value}), key), value)

    def test_profile_model_and_executable_reject_control_characters(self):
        for key in ('budget_id', 'executable', 'codex_home', 'model'):
            for value in ('', '  ', 'x\narg', 'x\rarg', 'x\0arg', 'x' * 501, 1, False):
                with self.subTest(key=key, value=value), self.assertRaises(ClubError):
                    self.load({key: value})
        config = self.load({'codex_home': None, 'model': None})
        self.assertIsNone(config.codex_home)
        self.assertIsNone(config.model)
        for key in ('budget_id', 'executable'):
            with self.subTest(key=key), self.assertRaises(ClubError):
                self.load({key: None})

    def test_explicit_model_and_separate_profile_are_preserved(self):
        config = self.load({'model': 'chosen-model', 'codex_home': '/srv/import-profile',
                            'executable': '/opt/codex/bin/codex'})
        self.assertEqual(config.model, 'chosen-model')
        self.assertEqual(config.codex_home, '/srv/import-profile')
        self.assertEqual(config.executable, '/opt/codex/bin/codex')

    def test_loaded_configuration_is_immutable_and_never_hot_reloads(self):
        self.load({'enabled': False, 'allowed_user_ids': [11]})
        with patch.dict(os.environ, {'BOOKCLUB_IMPORT_CONFIG_FILE': str(self.path)}):
            startup = load_import_config()
            self.path.write_text(json.dumps({'max_runs': 9, 'allowed_user_ids': [99]}), encoding='utf-8')
            with patch.dict(os.environ, {'BOOKCLUB_IMPORT_CONFIG_FILE': '/missing/new-config'}):
                self.assertEqual(startup.max_runs, 1)
                self.assertEqual(startup.allowed_user_ids, (11,))
            self.assertEqual(load_import_config().max_runs, 9)
        with self.assertRaises(FrozenInstanceError):
            startup.enabled = True
        with self.assertRaises(FrozenInstanceError):
            startup.allowed_user_ids += (99,)

    def test_unreadable_or_malformed_file_fails_closed(self):
        with self.assertRaises(ClubError):
            load_import_config(self.path)
        self.path.write_text('{ malformed', encoding='utf-8')
        with self.assertRaises(ClubError):
            load_import_config(self.path)

    def test_utf8_bom_config_is_supported(self):
        self.path.write_text('{"enabled": false}', encoding='utf-8-sig')
        self.assertFalse(load_import_config(self.path).enabled)
