"""Bounded local Codex CLI adapter; it never receives the Discord bot token.

Launch counts must be reserved by the caller before ``analyze``. A wall timeout
and launch quota are deliberately not described as a hard model-token budget.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
from typing import Any

from .store import ClubError


STDOUT_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
FINAL_LIMIT = 128 * 1024
PROMPT_LIMIT = 256 * 1024
LOGIN_TIMEOUT = 15 * 60
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
DEVICE_URL = 'https://auth.openai.com/codex/device'
# The current CLI displays groups of four and four/five uppercase characters.
DEVICE_CODE = re.compile(r'(?<![A-Z0-9-])([A-Z0-9]{4}-[A-Z0-9]{4,5})(?![A-Z0-9-])')
DISABLED_FEATURES = (
    'shell_tool', 'shell_snapshot', 'hooks', 'apps', 'plugins',
    'remote_plugin', 'multi_agent', 'multi_agent_v2', 'browser_use',
    'browser_use_external', 'computer_use', 'image_generation', 'view_image',
    'goals', 'memories', 'sleep_tool', 'code_mode', 'code_mode_host',
    'skill_mcp_dependency_install', 'skill_search', 'workspace_dependencies',
    'unbounded_connection_retries',
)
REQUIRED_FLAGS = ('--ignore-user-config', '--ephemeral', '--output-schema', '--output-last-message',
                  '--skip-git-repo-check', '--json', '--sandbox')
LOG = logging.getLogger(__name__)
EVENT_TYPES = frozenset({'thread.started', 'turn.started', 'turn.completed', 'turn.failed',
                         'item.started', 'item.updated', 'item.completed', 'error'})
PASSIVE_ITEMS = frozenset({'agent_message', 'reasoning', 'plan_update', 'todo_list'})
ITEM_TYPES = PASSIVE_ITEMS | {'command_execution', 'file_change', 'mcp_tool_call', 'web_search'}


class CodexOutputError(ClubError):
    """Only bounded, allowlisted metadata may leave the private CLI response."""

    def __init__(self, diagnostic: dict, message: str):
        self.diagnostic = diagnostic
        super().__init__(f'{message} Код диагностики: {diagnostic["code"]}.')


def _strict_json(value):
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = item
        return result

    def invalid_constant(_):
        raise ValueError('nonfinite_number')

    return json.loads(value, object_pairs_hook=unique_object, parse_constant=invalid_constant)


class CodexRunner:
    def __init__(self, executable: str = 'codex', *, home: Path,
                 timeout_seconds: int = 180, model: str | None = None):
        self.executable = executable
        if Path(home).expanduser().is_symlink():
            raise ClubError('Каталог Codex для импорта не должен быть символической ссылкой.')
        self.home = Path(home).expanduser().resolve()
        if self.home == (Path.home() / '.codex').resolve():
            raise ClubError('Для импорта нужен отдельный каталог Codex, не личный ~/.codex.')
        if not 1 <= timeout_seconds <= 600:
            raise ClubError('Таймаут Codex должен быть от 1 до 600 секунд.')
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.login_task: asyncio.Task | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._lock = asyncio.Lock()
        self._probed = False
        self._resolved_executable: str | None = None
        self._closed = False

    def _prepare_home(self):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.home.is_symlink() or (self.home / 'config.toml').exists() or (self.home / 'hooks.json').exists():
            raise ClubError('Каталог Codex для импорта должен быть отдельным и без config.toml/hooks.json.')
        if os.name != 'nt':
            self.home.chmod(0o700)
        (self.home / 'profile').mkdir(mode=0o700, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        # All other inherited values, including bot/API tokens, proxy endpoints,
        # shell startup variables and custom model providers, are excluded.
        keep = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'LANG', 'LC_ALL'}
        env = {key: value for key, value in os.environ.items() if key.upper() in keep}
        profile = str(self.home / 'profile')
        env.update(CODEX_HOME=str(self.home), HOME=profile, USERPROFILE=profile,
                   APPDATA=profile, LOCALAPPDATA=profile,
                   TEMP=tempfile.gettempdir(), TMP=tempfile.gettempdir(),
                   NO_COLOR='1', TERM='dumb')
        return env

    def _overrides(self) -> list[str]:
        # The built-in provider cannot be overridden in CLI 0.152.1. Its normal
        # HTTP/SSE retries remain bounded by our process deadline; do not add
        # model_providers.openai.* overrides or silently select a custom provider.
        config = ['model_provider="openai"', 'cli_auth_credentials_store="file"', 'forced_login_method="chatgpt"',
                  'web_search="disabled"', 'project_doc_max_bytes=0',
                  'tools.view_image=false', 'apps._default.enabled=false',
                  'shell_environment_policy.inherit="none"',
                  'sandbox_workspace_write.network_access=false',
                  'history.persistence="none"']
        config.extend(f'features.{name}=false' for name in DISABLED_FEATURES)
        return [arg for value in config for arg in ('-c', value)]

    async def _spawn(self, args: list[str], *, cwd: Path):
        if self._closed:
            raise ClubError('Подключение Codex остановлено; перезапустите бота для нового сеанса.')
        if self._resolved_executable is None:
            resolved = shutil.which(self.executable)
            if resolved is None:
                raise ClubError('Codex CLI не найден на машине бота. Установите его и перезапустите бота.')
            self._resolved_executable = str(Path(resolved).resolve())
        kwargs: dict[str, Any] = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        try:
            process = await asyncio.create_subprocess_exec(
                self._resolved_executable, *args, cwd=str(cwd), env=self._environment(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **kwargs)
        except OSError:
            raise ClubError('Не удалось запустить Codex CLI на машине бота.') from None
        self._processes.add(process)
        return process

    async def _stop(self, process):
        if process.returncode is None:
            if os.name == 'nt':
                # Use the trusted Windows executable directly, never a shell.
                taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
                with suppress(OSError, asyncio.TimeoutError):
                    killer = await asyncio.create_subprocess_exec(
                        str(taskkill), '/PID', str(process.pid), '/T', '/F',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await asyncio.wait_for(killer.wait(), 5)
            else:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 5)
        self._processes.discard(process)

    async def _read(self, stream, limit: int, callback=None) -> bytes:
        chunks, size = [], 0
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > limit:
                raise ClubError('Codex превысил допустимый размер ответа; операция остановлена.')
            chunks.append(chunk)
            if callback:
                callback(chunk)
        return b''.join(chunks)

    async def _capture(self, args, *, cwd, stdin=b'', timeout=15):
        process = await self._spawn(args, cwd=cwd)
        readers = []

        async def communicate():
            readers.extend([asyncio.create_task(self._read(process.stdout, STDOUT_LIMIT)),
                            asyncio.create_task(self._read(process.stderr, STDERR_LIMIT)),
                            asyncio.create_task(process.wait())])
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
            stdout, stderr, code = await asyncio.gather(*readers)
            return code, stdout, stderr

        try:
            return await asyncio.wait_for(communicate(), timeout)
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            await self._stop(process)
            await asyncio.gather(*readers, return_exceptions=True)

    async def _probe(self, cwd):
        if self._probed:
            return
        code, stdout, _ = await self._capture(['exec', '--help'], cwd=cwd)
        help_text = stdout.decode('utf-8', errors='replace')
        if code or any(flag not in help_text for flag in REQUIRED_FLAGS):
            raise ClubError('Установленный Codex CLI слишком старый: обновите его перед импортом.')
        code, stdout, _ = await self._capture(['features', 'list', *self._overrides()], cwd=cwd)
        flags = {line.split()[0]: line.split()[-1] for line in stdout.decode('utf-8', errors='replace').splitlines()
                 if len(line.split()) >= 3}
        if code or any(flags.get(name) != 'false' for name in DISABLED_FEATURES):
            raise ClubError('Эта версия Codex CLI не подтверждает ограничения импортера. Обновите CLI.')
        self._probed = True

    @staticmethod
    def parse_result(data: bytes, *, final_message: bytes | None = None) -> dict:
        """Read the exec JSONL protocol, never search arbitrary output for JSON.

        The documented --output-last-message file is the canonical final message.
        JSONL still has to confirm a successful, tool-free turn with valid usage.
        A missing file falls back to the documented completed agent-message item.
        Neither raw response text nor untrusted type/field names enter diagnostics.
        """
        final, usage, completed = None, None, False
        diagnostic = {'stdout_bytes': len(data), 'line': 0, 'events': {}, 'items': {},
                      'final_source': 'file' if final_message is not None else 'stream',
                      'final_bytes': len(final_message) if final_message is not None else 0}

        def fail(code, message='Codex вернул некорректный ответ; ничего не перенесено.'):
            diagnostic['code'] = code
            LOG.warning('Codex output rejected: %s', json.dumps(diagnostic, sort_keys=True))
            raise CodexOutputError(diagnostic, message) from None

        def count(group, value, allowed):
            label = value if isinstance(value, str) and value in allowed else 'unknown'
            diagnostic[group][label] = diagnostic[group].get(label, 0) + 1

        if len(data) > STDOUT_LIMIT:
            fail('stdout_limit')
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            fail('stream_encoding')
        # Only actual line separators delimit JSONL. str.splitlines() would also
        # split a valid JSON string containing U+2028/U+2029 from an essay.
        for line_number, line in enumerate(text.split('\n'), 1):
            diagnostic['line'] = line_number
            if not line.strip():
                continue
            try:
                event = _strict_json(line)
            except (ValueError, TypeError, RecursionError):
                fail('stream_json')
            if not isinstance(event, dict):
                fail('event_shape')
            kind = event.get('type')
            count('events', kind, EVENT_TYPES)
            if not isinstance(kind, str) or kind not in EVENT_TYPES:
                fail('event_type')
            if completed:
                fail('event_after_completion')
            if kind in {'error', 'turn.failed'}:
                fail('turn_failed', 'Codex не завершил анализ. Проверьте вход и доступный лимит аккаунта.')
            if kind in {'thread.started', 'turn.started'} and diagnostic['events'][kind] > 1:
                fail('multiple_turns')
            if kind.startswith('item.'):
                item = event.get('item')
                if not isinstance(item, dict):
                    fail('item_shape')
                item_kind = item.get('type')
                count('items', item_kind, ITEM_TYPES)
                # Inspect starts/updates too: an unfinished tool call must not
                # disappear just because it has no item.completed event.
                if not isinstance(item_kind, str) or item_kind not in PASSIVE_ITEMS:
                    fail('tool_or_unknown_item', 'Codex попытался использовать инструмент или вернул неизвестный тип данных; результат отклонён.')
                if kind == 'item.completed' and item_kind == 'agent_message':
                    if not isinstance(item.get('text'), str):
                        fail('agent_message_shape')
                    final = item['text']
            if kind == 'turn.completed':
                value = event.get('usage')
                if not isinstance(value, dict) or not all(
                        type(value.get(key)) is int and 0 <= value[key] < 2**63
                        for key in ('input_tokens', 'output_tokens')):
                    fail('usage_shape')
                for key in ('cached_input_tokens', 'reasoning_output_tokens'):
                    if key in value and (type(value[key]) is not int or not 0 <= value[key] < 2**63):
                        fail('usage_shape')
                usage = value['input_tokens'] + value['output_tokens']
                if usage >= 2**63:
                    fail('usage_shape')
                completed = True
        if not completed:
            fail('missing_completion')
        if final_message is not None:
            if len(final_message) > FINAL_LIMIT:
                fail('final_limit')
            try:
                final = final_message.decode('utf-8-sig')
            except UnicodeDecodeError:
                fail('final_encoding')
        if not isinstance(final, str) or not final.strip():
            fail('missing_final')
        try:
            diagnostic['final_bytes'] = len(final.encode('utf-8'))
        except UnicodeEncodeError:
            fail('final_encoding')
        if diagnostic['final_bytes'] > FINAL_LIMIT:
            fail('final_limit')
        try:
            output = _strict_json(final)
        except (ValueError, TypeError, RecursionError):
            fail('final_json')
        if not isinstance(output, dict):
            fail('final_shape')
        return {'output': output, 'usage_tokens': usage}

    @staticmethod
    def _read_final_message(path: Path) -> bytes | None:
        try:
            if not path.exists():
                return None
            if path.is_symlink():
                raise OSError
            with path.open('rb') as handle:
                return handle.read(FINAL_LIMIT + 1)
        except OSError:
            raise ClubError('Не удалось прочитать итоговый ответ Codex; ничего не перенесено.') from None

    async def analyze(self, prompt: str, schema: dict) -> dict:
        if self.login_task and not self.login_task.done():
            raise ClubError('Сначала завершите вход Codex.')
        if len(prompt.encode('utf-8')) > PROMPT_LIMIT:
            raise ClubError('Пакет сообщений слишком большой для одного запуска Codex.')
        async with self._lock:
            self._prepare_home()
            with tempfile.TemporaryDirectory(prefix='bookclub-codex-') as directory:
                cwd = Path(directory)
                try:
                    await self._probe(cwd)
                    schema_path = cwd / 'result.schema.json'
                    final_path = cwd / 'result.final.json'
                    schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
                    args = ['exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check',
                            '--sandbox', 'read-only', '--json', '--color', 'never',
                            '--output-schema', str(schema_path),
                            '--output-last-message', str(final_path), *self._overrides()]
                    if self.model:
                        args.extend(['--model', self.model])
                    args.append('-')
                    code, stdout, stderr = await self._capture(args, cwd=cwd, stdin=prompt.encode('utf-8'),
                                                             timeout=self.timeout_seconds)
                    if code:
                        diagnostic = {'code': 'process_exit', 'exit_code': code,
                                      'stdout_bytes': len(stdout), 'stderr_bytes': len(stderr)}
                        LOG.warning('Codex process failed: %s', json.dumps(diagnostic, sort_keys=True))
                        raise CodexOutputError(diagnostic,
                            'Codex не завершил анализ. Проверьте вход и доступный лимит аккаунта.')
                    return self.parse_result(stdout, final_message=self._read_final_message(final_path))
                except asyncio.TimeoutError:
                    raise ClubError('Время анализа Codex истекло; процесс остановлен. Лимит запуска уже израсходован.') from None

    async def begin_login(self) -> dict[str, str]:
        if self._lock.locked():
            raise ClubError('Дождитесь завершения текущего анализа Codex.')
        if self.login_task and not self.login_task.done():
            raise ClubError('Вход Codex уже ожидает подтверждения. Завершите его или дождитесь истечения кода.')
        self._prepare_home()
        ready = asyncio.get_running_loop().create_future()
        self.login_task = asyncio.create_task(self._login(ready))
        try:
            return await asyncio.wait_for(asyncio.shield(ready), 30)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            self.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.login_task
            if not ready.done():
                ready.cancel()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ClubError('Codex не выдал код входа за 30 секунд. Проверьте сеть и версию CLI.') from None

    async def _login(self, ready):
        buffer = bytearray()

        def observe(chunk):
            buffer.extend(chunk)
            if ready.done():
                return
            text = ANSI.sub('', buffer.decode('utf-8', errors='replace'))
            # Never relay arbitrary CLI URLs, messages, account data or tokens.
            urls = re.findall(r'https://[^\s<>"\x1b]+', text)
            uri = next((u for u in urls if u.rstrip('/') == DEVICE_URL), None)
            code = DEVICE_CODE.search(text)
            if uri and code:
                ready.set_result({'verification_uri': DEVICE_URL, 'user_code': code.group(1)})

        try:
            with tempfile.TemporaryDirectory(prefix='bookclub-codex-login-') as directory:
                process = await self._spawn(['login', '--device-auth', *self._overrides()], cwd=Path(directory))
                readers = []
                try:
                    process.stdin.close()
                    readers = [asyncio.create_task(self._read(process.stdout, STDERR_LIMIT, observe)),
                               asyncio.create_task(self._read(process.stderr, STDERR_LIMIT, observe)),
                               asyncio.create_task(process.wait())]
                    _, _, code = await asyncio.wait_for(asyncio.gather(*readers), LOGIN_TIMEOUT)
                    success = code == 0 and ready.done()
                    if not ready.done():
                        ready.set_exception(ClubError('Codex не выдал код входа. Включите device-code login в настройках безопасности ChatGPT и обновите CLI.'))
                    return success
                finally:
                    for task in readers:
                        if not task.done():
                            task.cancel()
                    await self._stop(process)
                    await asyncio.gather(*readers, return_exceptions=True)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception:
            if not ready.done():
                ready.set_exception(ClubError('Не удалось начать вход Codex. Проверьте установку CLI и доступ к OpenAI.'))
            return False

    async def login_status(self) -> bool:
        if self.login_task and not self.login_task.done():
            return False
        self._prepare_home()
        with tempfile.TemporaryDirectory(prefix='bookclub-codex-status-') as directory:
            try:
                code, _, _ = await self._capture(['login', 'status', *self._overrides()], cwd=Path(directory))
                return code == 0
            except asyncio.TimeoutError:
                raise ClubError('Codex не ответил на проверку входа.') from None

    async def close(self):
        self._closed = True
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.login_task
        for process in list(self._processes):
            await self._stop(process)
