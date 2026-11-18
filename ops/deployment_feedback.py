#!/usr/bin/env python3
"""Restricted SSH endpoint: public deployment diagnostics, never a remote shell."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

try:
    from .deployment_diagnostics import DeploymentDiagnostics, PHASES, RECOVERY, SHA, extract_diagnostics, public_reason
    from .update_bot import Runner, UpdateError, load_config, private_directory
except ImportError:
    from deployment_diagnostics import DeploymentDiagnostics, PHASES, RECOVERY, SHA, extract_diagnostics, public_reason
    from update_bot import Runner, UpdateError, load_config, private_directory


def read_json(path):
    if not path.exists():
        return {}
    if path.stat().st_size > 2_000_000:
        raise UpdateError('Diagnostic file is too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise UpdateError('Invalid diagnostic file')
    return value


def runtime(config, runner):
    ids = runner.run(['docker', 'ps', '--all', '--quiet', '--filter',
                      'label=com.docker.compose.project=' + config['project'], '--filter',
                      'label=com.docker.compose.service=' + config['service']]).splitlines()
    if len(ids) != 1:
        return {'healthy': False, 'revision': None}, None
    item = json.loads(runner.run(['docker', 'inspect', ids[0]]))[0]
    revision = item['Config'].get('Labels', {}).get('org.opencontainers.image.revision', '')
    revision = revision if SHA.fullmatch(revision) else None
    env = dict(value.split('=', 1) for value in item['Config'].get('Env', []))
    health_file = env.get('RESHUFFLE_HEALTH_FILE', '/data/runtime-health.json')
    healthy = False
    if revision and item['State'].get('Running') and health_file.startswith('/data/') and '..' not in Path(health_file).parts:
        args = ['docker', 'exec', item['Id'], 'python', '-m', 'bookclub.runtime_health',
                '--check', health_file, '--expected-sha', revision, '--max-age', '90', '--max-club-age', '300']
        if env.get('BOOKCLUB_ENABLED', '').lower() in {'true', 'yes', '1'}:
            args.append('--require-club')
        try:
            runner.run(args, timeout=15)
            healthy = True
        except UpdateError:
            pass
    return {'healthy': healthy, 'revision': revision}, item['Id']


def public_report(report, config):
    result = {}
    for field in ('state', 'failed_phase'):
        if report.get(field) in PHASES:
            result[field] = report[field]
    if report.get('recovery') in RECOVERY:
        result['recovery'] = report['recovery']
    if report.get('error'):
        result['reason'] = public_reason(str(report['error']))
    for field in ('started_at', 'finished_at'):
        if isinstance(report.get(field), (int, float)):
            result[field] = int(report[field])
    identifier = report.get('diagnostics_id', '')
    if re.fullmatch(r'[a-f0-9]{32}', identifier):
        # Only this generated public log is exported, never private-output.jsonl.
        result['log'] = read_json(Path(config['state_dir']) / 'logs' / identifier / 'public.json').get('entries', [])
    return result


def collect(config, commit, *, runner=None):
    if not SHA.fullmatch(commit):
        raise UpdateError('Invalid requested revision')
    runner = runner or Runner(30)
    state = Path(config['state_dir'])
    report = read_json(state / 'deployments' / (commit + '.json'))
    if not report:
        latest = read_json(state / 'last-report.json')
        if latest.get('target_commit') == commit:
            report = latest
    result = {'format': 1, 'commit': commit, 'observed_at': int(time.time()),
              'deployment': public_report(report, config)}
    current, container_id = runtime(config, runner)
    result['runtime'] = current
    auto = read_json(state / 'automatic-report.json')
    poll = read_json(state / 'poll-status.json')
    # A failed attempt stays a failure even when rollback restored a healthy bot.
    if report.get('state') == 'failed':
        result['state'] = 'failure'
    elif auto.get('target_commit') == commit and auto.get('state') in {'failed', 'blocked_release'}:
        result['state'] = 'failure'
        result['deployment']['reason'] = 'Deployment failed or was interrupted; explicit retry required.'
    elif poll.get('state') == 'error' and time.time() - poll.get('checked_at', 0) < 900:
        result['state'] = 'error'
        result['deployment']['reason'] = public_reason(poll.get('error', ''))
    elif report.get('state') in {'preflight', 'building', 'backup', 'deploying'}:
        result['state'] = 'pending'
    elif current['revision'] == commit:
        result['state'] = 'success' if current['healthy'] else 'failure'
        if not current['healthy']:
            result['deployment']['reason'] = 'Running revision failed Discord/club readiness checks.'
            if container_id:
                try:
                    output = subprocess.run(['docker', 'logs', '--tail', '200', container_id],
                                            capture_output=True, text=True, timeout=20)
                    result['runtime_diagnostics'] = extract_diagnostics(output.stdout + output.stderr, Path(config['repo']))
                except (OSError, subprocess.TimeoutExpired):
                    pass
    elif auto.get('state') == 'disabled':
        result['state'] = 'error'
        result['deployment']['reason'] = 'Automatic updates are disabled on the server.'
    else:
        result['state'] = 'pending'
        remote = runner.run(['git', '-C', config['repo'], 'rev-parse', config['remote'] + '/main'])
        if remote != commit:
            try:
                runner.run(['git', '-C', config['repo'], 'merge-base', '--is-ancestor', commit, remote])
                result['state'] = 'superseded'
                result['deployment']['reason'] = 'A newer main revision replaced this candidate.'
            except UpdateError:
                pass  # The server may not have fetched the requested revision yet.
    return result


def self_test(config, commit):
    """Fail a tiny isolated Docker build; never stop, mount or change the bot."""
    diagnostics = DeploymentDiagnostics(Path(config['state_dir']), Path(config['repo']))
    runner = Runner(60)
    runner.diagnostics = diagnostics
    diagnostics.phase('building')
    with tempfile.TemporaryDirectory(prefix='feedback-test-', dir=config['state_dir']) as directory:
        Path(directory, 'Dockerfile').write_text('FROM scratch\nCOPY missing-diagnostic-input /probe\n', encoding='utf-8')
        try:
            runner.run(['docker', 'build', '--network', 'none', '--progress', 'plain', directory])
        except UpdateError:
            diagnostics.phase('failed')
            return {'format': 1, 'commit': commit, 'observed_at': int(time.time()), 'state': 'failure',
                    'test': True, 'deployment': {'state': 'failed', 'failed_phase': 'building',
                    'reason': 'Intentional isolated build failure; production bot was not changed.',
                    'log': diagnostics.entries}}
    raise UpdateError('Diagnostic test unexpectedly succeeded')


def parse_command(command):
    match = re.fullmatch(r'(report|self-test|health|health-self-test) ([a-f0-9]{40})', command)
    if not match:
        raise UpdateError('Only approved diagnostic commands with one full commit SHA are allowed')
    return match.groups()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        mode, commit = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        config = load_config(args.config)
        private_directory(Path(config['state_dir']))
        if mode in {'health', 'health-self-test'}:
            try:
                from .runtime_monitor import health_report
            except ImportError:
                from runtime_monitor import health_report
            result = health_report(config, commit, test=mode == 'health-self-test')
        else:
            result = self_test(config, commit) if mode == 'self-test' else collect(config, commit)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (UpdateError, OSError, ValueError, KeyError, TypeError):
        # No tracebacks or raw host errors may cross this SSH boundary.
        print(json.dumps({'format': 1, 'state': 'unavailable', 'reason': 'Deployment diagnostics unavailable.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
