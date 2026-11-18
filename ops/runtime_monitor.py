"""Read-only runtime health independent of release success or the latest main SHA."""
from __future__ import annotations

import json
from pathlib import Path
import time

try:
    from .deployment_feedback import read_json
    from .deployment_diagnostics import SHA
    from .update_bot import Runner, UpdateError
except ImportError:
    from deployment_feedback import read_json
    from deployment_diagnostics import SHA
    from update_bot import Runner, UpdateError


REASONS = {'invalid-format', 'invalid-timestamps', 'future-timestamp', 'stale-heartbeat',
           'previous-process', 'invalid-pid', 'wrong-release', 'discord-disconnected',
           'invalid-club-state', 'club-disabled', 'club-not-initialized', 'invalid-reconciliation',
           'reconciliation-failed', 'reconciliation-pending', 'stale-reconciliation',
           'heartbeat-unreadable'}
PROBE = '''import contextlib,io,json,sys
from bookclub.runtime_health import main
output=io.StringIO()
with contextlib.redirect_stdout(output):
    code=main(sys.argv[1:])
text=output.getvalue().strip()
print(json.dumps({'healthy':code == 0,'reasons':text.removeprefix('unhealthy: ').split(',') if code else []}))
'''


def probe(config, runner):
    ids = runner.run(['docker', 'ps', '--all', '--quiet', '--filter',
                      'label=com.docker.compose.project=' + config['project'], '--filter',
                      'label=com.docker.compose.service=' + config['service']]).splitlines()
    if len(ids) != 1:
        return {'healthy': False, 'revision': None, 'reasons': ['container-missing-or-duplicate']}
    item = json.loads(runner.run(['docker', 'inspect', ids[0]]))[0]
    revision = item['Config'].get('Labels', {}).get('org.opencontainers.image.revision', '')
    revision = revision if SHA.fullmatch(revision) else None
    result = {'healthy': False, 'revision': revision, 'reasons': []}
    if not item['State'].get('Running') or item['State'].get('Restarting'):
        result['reasons'] = ['container-stopped-or-restarting']
        return result
    if not revision:
        result['reasons'] = ['unknown-running-revision']
        return result
    env = dict(value.split('=', 1) for value in item['Config'].get('Env', []))
    path = env.get('RESHUFFLE_HEALTH_FILE', '/data/runtime-health.json')
    if not path.startswith('/data/') or '..' in Path(path).parts:
        result['reasons'] = ['invalid-health-path']
        return result
    args = ['docker', 'exec', item['Id'], 'python', '-c', PROBE, '--check', path,
            '--expected-sha', revision, '--max-age', '90', '--max-club-age', '300']
    if env.get('BOOKCLUB_ENABLED', '').lower() in {'true', 'yes', '1'}:
        args.append('--require-club')
    data = json.loads(runner.run(args, timeout=20))
    if (type(data.get('healthy')) is not bool or not isinstance(data.get('reasons'), list)
            or any(reason not in REASONS for reason in data['reasons'])
            or data['healthy'] != (not data['reasons'])):
        raise UpdateError('Invalid runtime health result')
    result.update(healthy=data['healthy'], reasons=data['reasons'])
    return result


def updater_locked(state):
    """Check the real deployment lock; a stale JSON report cannot mask a crash."""
    import fcntl
    try:
        with (state / 'update.lock').open('r') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def planned_restart(config, now):
    state = Path(config['state_dir'])
    report = read_json(state / 'last-report.json')
    started = report.get('phase_started_at', report.get('started_at', 0))
    return (report.get('state') in {'backup', 'deploying'}
            and isinstance(started, (int, float))
            and 0 <= now - started <= min(config['health_timeout'] + 120, 600)
            and updater_locked(state))


def health_report(config, commit, *, runner=None, now=time.time, test=False):
    if not SHA.fullmatch(commit):
        raise UpdateError('Invalid requested revision')
    checked = now()
    result = {'format': 1, 'kind': 'runtime-health', 'commit': commit, 'observed_at': int(checked),
              'test': test}
    try:
        if test:
            # Evaluate a synthetic stale heartbeat through the real application checker.
            # No live file, container, database or service is modified.
            import sys
            sys.path.insert(0, config['repo'])
            from bookclub.runtime_health import check_health
            fixture = {'format': 1, 'updated_at': checked - 600, 'started_at': checked - 1200,
                       'pid': 1, 'deploy_sha': commit, 'connected': True, 'club_enabled': False}
            codes = check_health(fixture, expected_sha=commit, now=checked)
            current = {'healthy': not codes, 'revision': commit, 'reasons': codes}
        else:
            current = probe(config, runner or Runner(20))
        result['runtime'] = current
        result['state'] = 'healthy' if current['healthy'] else 'unhealthy'
        if not test and not current['healthy'] and planned_restart(config, checked):
            result['state'] = 'maintenance'
    except (UpdateError, OSError, ValueError, KeyError, TypeError):
        result['state'] = 'unavailable'
        result['reasons'] = ['server-reachable-runtime-diagnostics-unavailable']
    return result
