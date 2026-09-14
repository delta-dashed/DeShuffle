"""The CLI is always fake here: no login, model call or account usage."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bookclub.codex_runner import (CodexOutputError, CodexRunner, DEVICE_URL, DISABLED_FEATURES,
                                  FINAL_LIMIT, PROMPT_LIMIT, REQUIRED_FLAGS, STDOUT_LIMIT)
from bookclub.store import ClubError


def result_events(output=None):
    return b'\n'.join(json.dumps(event).encode() for event in [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(output or {'essays': []})}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 42, 'output_tokens': 8, 'reasoning_output_tokens': 3}},
    ])


def jsonl(*events):
    return b'\n'.join(json.dumps(event, ensure_ascii=False).encode() for event in events)


class FakeProcess:
    def __init__(self, stdout=b'', stderr=b'', code=0):
        self.returncode = code
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.stdin = Mock()
        self.stdin.drain = AsyncMock()
        self.wait = AsyncMock(return_value=code)
        self.pid = 123456789


class CodexRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = CodexRunner(home=Path(self.tmp.name) / 'profile', model='configured-model')
        self.addAsyncCleanup(self.runner.close)

    def test_child_environment_excludes_bot_and_provider_credentials(self):
        with patch.dict(os.environ, {'DISCORD_TOKEN': 'discord-secret', 'OPENAI_API_KEY': 'api-secret',
                                     'CODEX_ACCESS_TOKEN': 'auth-secret', 'GITHUB_TOKEN': 'git-secret',
                                     'HTTP_PROXY': 'http://untrusted.invalid', 'BASH_ENV': '/tmp/script'}):
            env = self.runner._environment()
        self.assertFalse({'DISCORD_TOKEN', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'GITHUB_TOKEN',
                          'HTTP_PROXY', 'BASH_ENV'} & env.keys())
        self.assertEqual(env['CODEX_HOME'], str(self.runner.home))
        self.assertEqual(env['HOME'], str(self.runner.home / 'profile'))

    def test_rejects_personal_codex_profile(self):
        with self.assertRaises(ClubError):
            CodexRunner(home=Path.home() / '.codex')

    def test_rejects_user_configuration_in_profile(self):
        self.runner.home.mkdir()
        (self.runner.home / 'config.toml').write_text('features.hooks=true')
        with self.assertRaises(ClubError):
            self.runner._prepare_home()

    def test_private_profile_on_posix(self):
        self.runner._prepare_home()
        if os.name != 'nt':
            self.assertEqual(self.runner.home.stat().st_mode & 0o777, 0o700)

    async def test_analyze_uses_stdin_isolated_directory_schema_and_disables(self):
        calls = []

        async def capture(args, *, cwd, stdin=b'', timeout=15):
            calls.append((args, cwd, stdin))
            self.assertNotEqual(cwd, Path.cwd())
            self.assertEqual(json.loads((cwd / 'result.schema.json').read_text()), {'type': 'object'})
            return 0, result_events(), b'credentials must never be returned'

        with patch.object(self.runner, '_probe', AsyncMock()), patch.object(self.runner, '_capture', side_effect=capture):
            result = await self.runner.analyze('untrusted chat text', {'type': 'object'})
        self.assertEqual(result, {'output': {'essays': []}, 'usage_tokens': 50})
        args, cwd, stdin = calls[0]
        self.assertEqual(stdin, b'untrusted chat text')
        self.assertEqual(args[-1], '-')
        self.assertIn('--ignore-user-config', args)
        self.assertIn('--output-last-message', args)
        self.assertIn('read-only', args)
        self.assertIn('web_search="disabled"', args)
        self.assertIn('features.hooks=false', args)
        self.assertIn('features.shell_tool=false', args)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('features.unbounded_connection_retries=false', args)
        self.assertFalse(any(arg.startswith('model_providers.openai.') for arg in args))
        self.assertIn('configured-model', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.assertNotIn('--ignore-rules', args)
        self.assertFalse(cwd.exists())

    async def test_optional_model_is_not_overridden(self):
        self.runner.model = None
        capture = AsyncMock(return_value=(0, result_events(), b''))
        with patch.object(self.runner, '_probe', AsyncMock()), patch.object(self.runner, '_capture', capture):
            await self.runner.analyze('text', {})
        self.assertNotIn('--model', capture.call_args.args[0])

    async def test_old_cli_fails_before_model_invocation(self):
        capture = AsyncMock(return_value=(0, b'old help text', b''))
        with patch.object(self.runner, '_capture', capture), self.assertRaises(ClubError):
            await self.runner.analyze('text', {})
        self.assertEqual(capture.await_count, 1)
        self.assertEqual(capture.call_args.args[0], ['exec', '--help'])

    async def test_feature_probe_requires_all_restrictions_confirmed(self):
        help_output = ' '.join(REQUIRED_FLAGS).encode()
        features = '\n'.join(f'{name} stable false' for name in DISABLED_FEATURES).encode()
        with patch.object(self.runner, '_capture', AsyncMock(side_effect=[(0, help_output, b''), (0, features, b'')])):
            await self.runner._probe(Path(self.tmp.name))
        self.assertTrue(self.runner._probed)
        self.runner._probed = False
        features = features.replace(b'shell_tool stable false', b'shell_tool stable true')
        with patch.object(self.runner, '_capture', AsyncMock(side_effect=[(0, help_output, b''), (0, features, b'')])):
            with self.assertRaises(ClubError):
                await self.runner._probe(Path(self.tmp.name))

    async def test_input_limit_prevents_process_launch(self):
        with patch.object(self.runner, '_spawn', AsyncMock()) as spawn, self.assertRaises(ClubError):
            await self.runner.analyze('x' * (PROMPT_LIMIT + 1), {})
        spawn.assert_not_called()

    def test_usage_does_not_double_count_reasoning(self):
        self.assertEqual(CodexRunner.parse_result(result_events())['usage_tokens'], 50)

    def test_invalid_failed_or_nonobject_result_is_rejected(self):
        for data in (b'not json', b'{}', b'{"type":"turn.failed","error":"secret"}',
                     result_events(['not an object']), result_events({'value': 'x' * FINAL_LIMIT})):
            with self.subTest(data=data[:80]), self.assertRaises(ClubError):
                CodexRunner.parse_result(data)

    def test_tool_result_is_rejected(self):
        tool = b'{"type":"item.completed","item":{"type":"command_execution"}}\n'
        with self.assertRaises(ClubError):
            CodexRunner.parse_result(tool + result_events())

    def test_documented_full_stream_allows_passive_progress(self):
        data = jsonl(
            {'type': 'thread.started', 'thread_id': 'fixture-thread'},
            {'type': 'turn.started'},
            {'type': 'item.started', 'item': {'id': 'r', 'type': 'reasoning', 'text': ''}},
            {'type': 'item.completed', 'item': {'id': 'r', 'type': 'reasoning', 'text': 'private'}},
            {'type': 'item.updated', 'item': {'id': 'p', 'type': 'todo_list', 'items': []}},
            {'type': 'item.completed', 'item': {'id': 'a', 'type': 'agent_message', 'text': 'Progress only'}},
        ) + b'\n' + result_events()
        self.assertEqual(CodexRunner.parse_result(data), {'output': {'essays': []}, 'usage_tokens': 50})

    def test_last_message_file_is_canonical_when_stream_has_only_progress(self):
        data = jsonl(
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Progress only'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 3}},
        )
        parsed = CodexRunner.parse_result(data, final_message=b'{"essays":[]}')
        self.assertEqual(parsed, {'output': {'essays': []}, 'usage_tokens': 7})

    def test_final_file_never_bypasses_terminal_or_tool_validation(self):
        final = b'{"essays":[]}'
        for stream in (b'', b'not json', b'{"type":"turn.failed"}',
                       b'{"type":"item.started","item":{"type":"command_execution"}}\n' + result_events()):
            with self.subTest(stream=stream[:60]), self.assertRaises(CodexOutputError):
                CodexRunner.parse_result(stream, final_message=final)

    async def test_analyze_reads_bounded_final_file_before_cleaning_up(self):
        async def capture(args, *, cwd, **kwargs):
            path = Path(args[args.index('--output-last-message') + 1])
            self.assertEqual(path.parent, cwd)
            path.write_bytes(b'{"essays":[],"notes":"fixture"}')
            return 0, result_events(), b'private-stderr'

        with patch.object(self.runner, '_probe', AsyncMock()), patch.object(self.runner, '_capture', capture):
            result = await self.runner.analyze('private-prompt', {})
        self.assertEqual(result['output'], {'essays': [], 'notes': 'fixture'})
        self.assertFalse(list(Path(self.tmp.name).rglob('result.final.json')))

    def test_final_file_limit_is_bounded_and_does_not_fall_back(self):
        path = Path(self.tmp.name) / 'final.json'
        path.write_bytes(b'x' * (FINAL_LIMIT + 10))
        data = CodexRunner._read_final_message(path)
        self.assertEqual(len(data), FINAL_LIMIT + 1)
        with self.assertRaises(CodexOutputError) as raised:
            CodexRunner.parse_result(result_events(), final_message=data)
        self.assertEqual(raised.exception.diagnostic['code'], 'final_limit')

    def test_existing_invalid_final_file_never_falls_back_to_stream(self):
        for final in (b'', b'not json', b'[]', b'\xff', b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(final=final), self.assertRaises(CodexOutputError):
                CodexRunner.parse_result(result_events(), final_message=final)

    def test_utf8_bom_crlf_and_unicode_line_separators(self):
        output = {'essays': [], 'notes': 'a\u2028b\u2029c'}
        stream = jsonl(
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(output, ensure_ascii=False)}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}},
        ).replace(b'\n', b'\r\n')
        self.assertEqual(CodexRunner.parse_result(b'\xef\xbb\xbf' + stream)['output'], output)

    def test_unknown_event_and_item_names_are_not_logged(self):
        secret = 'secret-token-essay-content@example.invalid'
        for data in (jsonl({'type': secret, secret: secret}),
                     jsonl({'type': 'item.updated', 'item': {'type': secret, 'text': secret}}),
                     jsonl({'type': 'error', 'message': secret}),
                     result_events({'notes': secret}) + b'\n' + jsonl({'type': secret})):
            with self.subTest(data=data[:40]), self.assertLogs('bookclub.codex_runner', level='WARNING') as logs:
                with self.assertRaises(CodexOutputError) as raised:
                    CodexRunner.parse_result(data)
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(secret, json.dumps(raised.exception.diagnostic))
            self.assertNotIn(secret, '\n'.join(logs.output))
            self.assertEqual(raised.exception.diagnostic['stdout_bytes'], len(data))

    def test_tool_starts_and_updates_are_rejected_even_without_completion(self):
        for event in ('item.started', 'item.updated'):
            for item in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
                data = jsonl({'type': event, 'item': {'type': item}}) + b'\n' + result_events()
                with self.subTest(event=event, item=item), self.assertRaises(CodexOutputError):
                    CodexRunner.parse_result(data)

    def test_invalid_usage_never_succeeds_with_unknown_accounting(self):
        prefix = result_events().split(b'\n')[0] + b'\n'
        for usage in (None, [], {}, {'input_tokens': 1}, {'input_tokens': True, 'output_tokens': 1},
                      {'input_tokens': -1, 'output_tokens': 1}, {'input_tokens': 1.5, 'output_tokens': 1},
                      {'input_tokens': 1, 'output_tokens': 2**63 - 1},
                      {'input_tokens': 1, 'output_tokens': 1, 'cached_input_tokens': '1'}):
            with self.subTest(usage=usage), self.assertRaises(CodexOutputError) as raised:
                CodexRunner.parse_result(prefix + jsonl({'type': 'turn.completed', 'usage': usage}))
            self.assertEqual(raised.exception.diagnostic['code'], 'usage_shape')

    def test_missing_duplicate_and_trailing_terminal_events_fail_closed(self):
        for data in (result_events().split(b'\n')[0], result_events() + b'\n' + result_events(),
                     result_events() + b'\n' + jsonl({'type': 'turn.started'}),
                     jsonl({'type': 'turn.started'}, {'type': 'turn.started'}) + b'\n' + result_events()):
            with self.subTest(data=data[:40]), self.assertRaises(CodexOutputError):
                CodexRunner.parse_result(data)

    def test_wrong_shapes_and_wrappers_are_not_searched_for_embedded_json(self):
        for data in (b'[]', b'null', b'{"type":[]}', b'{"type":"item.completed","item":[]}',
                     b'{"type":"item.completed","item":{"type":"agent_message","text":{}}}',
                     b'{"type":"wrapper","event":{"type":"turn.completed"}}',
                     b'{"type":"turn.completed","type":"turn.failed"}',
                     b'\xff', b'{"type":"item.completed"',
                     b'x' * (STDOUT_LIMIT + 1)):
            with self.subTest(data=data[:60]), self.assertRaises(CodexOutputError):
                CodexRunner.parse_result(data)

    def test_no_final_message_and_markdown_wrapped_json_are_rejected(self):
        terminal = result_events().split(b'\n')[1]
        for data in (terminal, jsonl({'type': 'item.completed', 'item': {'type': 'agent_message',
                         'text': '```json\n{"essays":[]}\n```'}}) + b'\n' + terminal):
            with self.subTest(data=data[:40]), self.assertRaises(CodexOutputError):
                CodexRunner.parse_result(data)

    async def test_timeout_is_reported_without_raw_output(self):
        with patch.object(self.runner, '_probe', AsyncMock()), \
                patch.object(self.runner, '_capture', AsyncMock(side_effect=TimeoutError)):
            with self.assertRaisesRegex(ClubError, 'Время анализа'):
                await self.runner.analyze('text', {})

    async def test_failed_process_diagnostics_never_include_cli_text(self):
        secret = b'private-essay-or-account-token'
        with patch.object(self.runner, '_probe', AsyncMock()), \
                patch.object(self.runner, '_capture', AsyncMock(return_value=(4, secret, secret))), \
                self.assertLogs('bookclub.codex_runner', level='WARNING') as logs:
            with self.assertRaises(CodexOutputError) as raised:
                await self.runner.analyze('text', {})
        self.assertEqual(raised.exception.diagnostic,
                         {'code': 'process_exit', 'exit_code': 4,
                          'stdout_bytes': len(secret), 'stderr_bytes': len(secret)})
        self.assertNotIn(secret.decode(), '\n'.join(logs.output) + str(raised.exception))

    async def test_capture_stops_process_when_output_limit_exceeded(self):
        process = FakeProcess(stdout=b'x' * 100)
        with patch.object(self.runner, '_spawn', AsyncMock(return_value=process)), \
                patch.object(self.runner, '_stop', AsyncMock()) as stop, \
                patch('bookclub.codex_runner.STDOUT_LIMIT', 20):
            with self.assertRaises(ClubError):
                await self.runner._capture(['exec'], cwd=Path(self.tmp.name))
            stop.assert_awaited_once_with(process)

    async def test_capture_timeout_cleans_up_pending_readers_and_process(self):
        process = FakeProcess()

        async def pending_exit():
            await asyncio.sleep(100)

        process.wait = pending_exit
        with patch.object(self.runner, '_spawn', AsyncMock(return_value=process)), \
                patch.object(self.runner, '_stop', AsyncMock()) as stop:
            with self.assertRaises(asyncio.TimeoutError):
                await self.runner._capture(['exec'], cwd=Path(self.tmp.name), timeout=0.01)
            stop.assert_awaited_once_with(process)

    async def test_expired_login_is_stopped_after_code_was_delivered(self):
        process = FakeProcess(stderr=(DEVICE_URL + '\nABCD-1234\n').encode())

        async def pending_exit():
            await asyncio.sleep(100)

        process.wait = pending_exit
        with patch.object(self.runner, '_spawn', AsyncMock(return_value=process)), \
                patch.object(self.runner, '_stop', AsyncMock()) as stop, \
                patch('bookclub.codex_runner.LOGIN_TIMEOUT', 0.01):
            self.assertEqual((await self.runner.begin_login())['user_code'], 'ABCD-1234')
            self.assertFalse(await self.runner.login_task)
            stop.assert_awaited_once_with(process)

    async def test_login_only_relays_official_url_and_device_code(self):
        process = FakeProcess(stderr=(f'\x1b[32m{DEVICE_URL}\x1b[0m\nABCD-12345\nprivate-account-details').encode())
        with patch.object(self.runner, '_spawn', AsyncMock(return_value=process)), \
                patch.object(self.runner, '_stop', AsyncMock()):
            info = await self.runner.begin_login()
            self.assertEqual(info, {'verification_uri': DEVICE_URL, 'user_code': 'ABCD-12345'})
            self.assertTrue(await self.runner.login_task)

    async def test_login_rejects_other_verification_host_and_hides_raw_error(self):
        process = FakeProcess(stderr=b'https://evil.invalid/codex/device\nABCD-1234\nsecret', code=1)
        with patch.object(self.runner, '_spawn', AsyncMock(return_value=process)), \
                patch.object(self.runner, '_stop', AsyncMock()):
            with self.assertRaises(ClubError) as raised:
                await self.runner.begin_login()
            self.assertNotIn('secret', str(raised.exception))
            self.assertNotIn('evil.invalid', str(raised.exception))
            self.assertFalse(await self.runner.login_task)

    async def test_login_status_returns_only_boolean(self):
        with patch.object(self.runner, '_capture', AsyncMock(return_value=(0, b'secret', b'secret'))):
            self.assertIs(await self.runner.login_status(), True)
        with patch.object(self.runner, '_capture', AsyncMock(return_value=(1, b'secret', b'secret'))):
            self.assertIs(await self.runner.login_status(), False)

    async def test_close_cancels_login_and_prevents_future_processes(self):
        self.runner.login_task = asyncio.create_task(asyncio.sleep(100))
        await self.runner.close()
        self.assertTrue(self.runner.login_task.cancelled())
        with self.assertRaises(ClubError):
            await self.runner._spawn(['exec'], cwd=Path(self.tmp.name))
