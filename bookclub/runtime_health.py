"""Private, local runtime heartbeat for container and deployment health checks.

The file contains operational state only. In particular, exception messages,
Discord content, settings and credentials never belong in this interface.
This module intentionally uses only the standard library so its check can run
without importing the bot, loading .env or contacting Discord.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Optional


FORMAT_VERSION = 1
HEARTBEAT_SECONDS = 15
log = logging.getLogger(__name__)
_ERROR_CODE = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,95}\Z')


class RuntimeHealth:
    """A single-process tracker; synchronous updates never yield bot state."""

    def __init__(self, path, *, deploy_sha='', club_enabled=False,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        self.started_at = clock()
        self.deploy_sha = deploy_sha
        self.club_enabled = bool(club_enabled)
        self.connected = False
        self.schema_version = None
        self.initialized = False
        self.guilds = {}
        self._task = None
        self._write_failed = False
        self.write()

    def snapshot(self):
        return {
            'format': FORMAT_VERSION,
            'updated_at': self.clock(),
            'started_at': self.started_at,
            'pid': os.getpid(),
            'deploy_sha': self.deploy_sha,
            'connected': self.connected,
            'club_enabled': self.club_enabled,
            'schema_version': self.schema_version,
            'reconciliation': {'initialized': self.initialized,
                               'guilds': {key: dict(value) for key, value in self.guilds.items()}},
        }

    def write(self):
        """Replace atomically, keeping the previous complete file on failure."""
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.runtime-health-', dir=self.path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(self.snapshot(), stream, ensure_ascii=True, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._write_failed = False
            return True
        except (OSError, TypeError, ValueError) as exc:
            if not self._write_failed:
                log.error('Runtime health file write failed (%s)', type(exc).__name__)
            self._write_failed = True
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def set_connected(self, connected):
        self.connected = bool(connected)
        self.write()

    def configure_club(self, guild_ids, schema_version):
        """Only enabled, published guilds require a completed reconciliation."""
        expected = {str(int(guild_id)) for guild_id in guild_ids}
        self.guilds = {
            key: self.guilds.get(key, {'state': 'pending', 'last_attempt_at': None,
                                      'last_success_at': None, 'error': None})
            for key in sorted(expected)
        }
        self.schema_version = int(schema_version)
        self.initialized = True
        self.write()

    def begin_reconciliation(self, guild_id):
        state = self.guilds.get(str(guild_id))
        if state is not None:
            state.update(state='running', last_attempt_at=self.clock())
            self.write()

    def finish_reconciliation(self, guild_id, error=None):
        state = self.guilds.get(str(guild_id))
        if state is None:
            return
        if error is None:
            state.update(state='ok', last_success_at=self.clock(), error=None)
        else:
            # Callers pass a category or exception *type*, never its message.
            code = error if isinstance(error, str) else type(error).__name__
            state.update(state='error', error=code if _ERROR_CODE.fullmatch(code) else 'ReconciliationError')
        self.write()

    def start(self, bot):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._heartbeat(bot), name='runtime-health-heartbeat')

    def stop(self):
        if self._task is not None:
            self._task.cancel()
        self.set_connected(False)

    async def _heartbeat(self, bot):
        try:
            while not bot.is_closed():
                # discord.py retains is_ready() across a resumable disconnect.
                # Only the ready/resumed gateway events may restore connection.
                self.set_connected(self.connected and bot.is_ready())
                await asyncio.sleep(HEARTBEAT_SECONDS)
        finally:
            self.set_connected(False)


def bot_health(bot) -> Optional[RuntimeHealth]:
    """Keep mocks and unrelated extension attributes out of the health path."""
    tracker = getattr(bot, 'runtime_health', None)
    return tracker if isinstance(tracker, RuntimeHealth) else None


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def check_health(payload, *, expected_sha='', max_age=90, max_club_age=300,
                 require_club=False, started_after=None, now=None):
    """Return public reason codes; an empty list means ready for this release."""
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('format') != FORMAT_VERSION:
        return ['invalid-format']
    updated = payload.get('updated_at')
    started = payload.get('started_at')
    if not _timestamp(updated) or not _timestamp(started) or updated < started:
        return ['invalid-timestamps']
    errors = []
    if updated > now + 30 or started > now + 30:
        errors.append('future-timestamp')
    if now - updated > max_age:
        errors.append('stale-heartbeat')
    if started_after is not None and started < started_after:
        errors.append('previous-process')
    if type(payload.get('pid')) is not int or payload['pid'] <= 0:
        errors.append('invalid-pid')
    if expected_sha and payload.get('deploy_sha') != expected_sha:
        errors.append('wrong-release')
    if payload.get('connected') is not True:
        errors.append('discord-disconnected')
    enabled = payload.get('club_enabled')
    if type(enabled) is not bool:
        errors.append('invalid-club-state')
    if require_club and enabled is not True:
        errors.append('club-disabled')
    if enabled is True:
        reconciliation = payload.get('reconciliation')
        schema = payload.get('schema_version')
        if (not isinstance(reconciliation, dict) or reconciliation.get('initialized') is not True
                or type(schema) is not int or schema <= 0):
            errors.append('club-not-initialized')
            return errors
        guilds = reconciliation.get('guilds')
        if not isinstance(guilds, dict):
            errors.append('invalid-reconciliation')
            return errors
        for state in guilds.values():
            if not isinstance(state, dict) or state.get('state') not in {'pending', 'running', 'ok', 'error'}:
                errors.append('invalid-reconciliation')
                break
            if state.get('error') is not None or state['state'] == 'error':
                errors.append('reconciliation-failed')
                break
            success = state.get('last_success_at')
            if not _timestamp(success) or success < started:
                errors.append('reconciliation-pending')
                break
            if success > now + 30 or now - success > max_club_age:
                errors.append('stale-reconciliation')
                break
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check local bot heartbeat without Discord or model calls.')
    parser.add_argument('--check', required=True, type=Path)
    parser.add_argument('--expected-sha', default=os.getenv('RESHUFFLE_DEPLOY_SHA', ''))
    parser.add_argument('--max-age', type=float, default=90)
    parser.add_argument('--max-club-age', type=float, default=300)
    parser.add_argument('--started-after', type=float)
    parser.add_argument('--require-club', action='store_true')
    args = parser.parse_args(argv)
    if args.max_age <= 0 or not math.isfinite(args.max_age) or args.max_club_age <= 0 or not math.isfinite(args.max_club_age):
        parser.error('age limits must be positive finite numbers')
    if args.started_after is not None and not _timestamp(args.started_after):
        parser.error('started-after must be a positive finite timestamp')
    try:
        # A corrupt/misconfigured file must not allocate arbitrary amounts of memory.
        with args.check.open('r', encoding='utf-8') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('oversized')
        payload = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        print('unhealthy: heartbeat-unreadable')
        return 1
    errors = check_health(payload, expected_sha=args.expected_sha, max_age=args.max_age,
                          max_club_age=args.max_club_age, require_club=args.require_club,
                          started_after=args.started_after)
    print('unhealthy: ' + ','.join(errors) if errors else 'healthy')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
