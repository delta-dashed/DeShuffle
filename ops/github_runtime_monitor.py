#!/usr/bin/env python3
"""Periodic external health check; GitHub remains able to report a dead server."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

try:
    from .github_deployment_observer import ssh_report
except ImportError:
    from github_deployment_observer import ssh_report


def monitor(commit, directory, *, fetch=ssh_report, mode='check', sleep=time.sleep,
            monotonic=time.monotonic, now=time.time, timeout=420):
    deadline = monotonic() + timeout
    samples, failures = [], 0
    while monotonic() < deadline:
        try:
            sample = fetch(directory, commit, 'health-self-test' if mode == 'unhealthy-test' else 'health',
                           unreachable_test=mode == 'unreachable-test')
            if (not isinstance(sample.get('observed_at'), int)
                    or not now() - 120 <= sample['observed_at'] <= now() + 30):
                raise ValueError('Stale monitor response')
            if sample['state'] == 'healthy' and sample.get('runtime', {}).get('healthy') is not True:
                raise ValueError('Contradictory monitor response')
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            sample = {'state': 'unreachable', 'observed_at': int(now()),
                      'reasons': ['server-or-restricted-ssh-endpoint-unreachable']}
        samples.append(sample)
        print('Runtime probe: ' + sample['state'], flush=True)
        if sample['state'] == 'healthy':
            break
        if sample['state'] == 'maintenance':
            failures = 0  # Wait for a bounded, genuinely locked deployment to finish.
        else:
            failures += 1
            if failures >= 3:
                break
        sleep(15)
    last = samples[-1] if samples else {'state': 'unavailable'}
    state = last['state']
    if state != 'healthy' and failures < 3:
        state = 'unavailable'  # Never turn an unconfirmed startup into a green result.
        last = {**last, 'reasons': ['readiness-not-confirmed-before-monitor-deadline']}
    return {'format': 1, 'kind': 'periodic-runtime-health', 'commit': commit, 'mode': mode,
            'checked_at': int(now()), 'state': state, 'runtime': last.get('runtime', {}),
            'reasons': last.get('reasons', last.get('runtime', {}).get('reasons', [])), 'samples': samples}


def render_log(report):
    checked = datetime.fromtimestamp(report['checked_at'], timezone.utc).isoformat()
    lines = ['Checked at (UTC): ' + checked, 'Current bot health: ' + report['state'],
             'Running revision: ' + str(report.get('runtime', {}).get('revision') or 'unknown'),
             'Probe count: ' + str(len(report.get('samples', [])))]
    if report['mode'] != 'check':
        lines.append('Isolated diagnostic test: ' + report['mode'] + '; production was not changed.')
    lines.extend('Reason: ' + reason for reason in report.get('reasons', []))
    lines.append('This result describes runtime health, not deployment history.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('check', 'unhealthy-test', 'unreachable-test'), default='check')
    args = parser.parse_args(argv)
    os.umask(0o077)
    commit = os.environ['GITHUB_SHA']
    if (not re.fullmatch(r'[a-f0-9]{40}', commit) or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') not in {'schedule', 'workflow_dispatch'}):
        raise ValueError('Runtime monitoring only runs on trusted main')
    directory = Path('runtime-health-report')
    directory.mkdir(exist_ok=True)
    report = {'format': 1, 'kind': 'periodic-runtime-health', 'commit': commit, 'mode': args.mode,
              'state': 'unavailable', 'checked_at': int(time.time()),
              'reasons': ['monitor-configuration-or-execution-error'], 'samples': []}
    try:
        with tempfile.TemporaryDirectory(prefix='health-ssh-') as temporary:
            secret_dir = Path(temporary)
            for name, variable in [('key', 'DEPLOY_FEEDBACK_KEY'), ('known_hosts', 'DEPLOY_FEEDBACK_KNOWN_HOSTS')]:
                path = secret_dir / name
                value = os.environ[variable].strip()
                if not value:
                    raise ValueError('Missing monitor secret')
                path.write_text(value + '\n')
                path.chmod(0o600)
            report = monitor(commit, secret_dir, mode=args.mode)
    except Exception:
        print('Runtime monitoring could not complete; private exception details are omitted.')
    (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    log = render_log(report)
    (directory / 'diagnostics.log').write_text(log, encoding='utf-8')
    print('\n'.join('  ' + line for line in log.splitlines()), flush=True)
    summary = '# Bot runtime health\n\n' + '\n'.join('    ' + line for line in log.splitlines()) + '\n'
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
        stream.write(summary)
    if report['state'] != 'healthy':
        print('::error title=Bot runtime health::' + report['state'] + '; see the diagnostic report.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
