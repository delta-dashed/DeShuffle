#!/usr/bin/env python3
"""GitHub Actions observer; the server timer remains the sole automatic deployer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


REPOSITORY = 'delta-dashed/DeShuffle'


def target(event, event_name, default_sha):
    if event_name == 'workflow_run':
        run = event['workflow_run']
        if (run.get('event') not in {'push', 'workflow_dispatch'} or run.get('head_branch') != 'main'
                or run.get('head_repository', {}).get('full_name') != REPOSITORY
                or run.get('path', '').split('@', 1)[0] != '.github/workflows/ci.yml'):
            raise ValueError('Only trusted main CI runs may be observed')
        commit, conclusion = run['head_sha'], run.get('conclusion')
    elif event_name == 'workflow_dispatch' and os.environ.get('GITHUB_REF') == 'refs/heads/main':
        commit, conclusion = default_sha, 'success'
    else:
        raise ValueError('Unsupported observation trigger')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Invalid commit')
    return commit, conclusion


def api(path, body):
    request = Request('https://api.github.com/repos/' + REPOSITORY + path,
                      data=json.dumps(body).encode(), method='POST', headers={
                          'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                          'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                          'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'DeShuffle-deployment-observer'})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def ssh_report(directory, commit, mode, *, unreachable_test=False):
    commands = {'observe': 'report', 'self-test': 'self-test', 'health': 'health',
                'health-self-test': 'health-self-test'}
    if mode not in commands:
        raise ValueError('Invalid diagnostic mode')
    host = '127.0.0.1' if unreachable_test else os.environ['DEPLOY_FEEDBACK_HOST']
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host):
        raise ValueError('Invalid host configuration')
    result = subprocess.run([
        'ssh', '-T', '-p', '1' if unreachable_test else '22', '-i', str(directory / 'key'), '-o', 'IdentitiesOnly=yes',
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o',
        'UserKnownHostsFile=' + str(directory / 'known_hosts'), '-o', 'ConnectTimeout=15',
        '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
        'root@' + host, commands[mode] + ' ' + commit,
    ], capture_output=True, text=True, timeout=60 if mode.startswith('health') else 90)
    if result.returncode or len(result.stdout) > 1_000_000:
        raise ValueError('Restricted server diagnostics unavailable')
    report = json.loads(result.stdout)
    health = mode.startswith('health')
    states = {'healthy', 'unhealthy', 'unavailable', 'maintenance'} if health else {
        'pending', 'success', 'failure', 'error', 'superseded'}
    if (report.get('format') != 1 or report.get('commit') != commit or report.get('state') not in states
            or bool(report.get('test')) != (mode in {'self-test', 'health-self-test'})
            or (health and report.get('kind') != 'runtime-health')):
        raise ValueError('Invalid server diagnostic response')
    return report


def render_log(report):
    deployment = report.get('deployment', {})
    lines = ['Revision: ' + report['commit'], 'Result: ' + report['state']]
    for field in ('state', 'failed_phase', 'reason', 'recovery'):
        if field in deployment:
            lines.append(field + ': ' + str(deployment[field]))
    for entry in deployment.get('log', []):
        if 'phase' in entry:
            lines.append('Phase: ' + entry['phase'])
        elif entry.get('exit_code') != 0 or entry.get('diagnostics'):
            lines.append('Command: ' + str(entry.get('tool')) + '; exit=' + str(entry.get('exit_code'))
                         + '; timeout=' + str(entry.get('timeout', False)))
            lines.extend('  ' + str(item) for item in entry.get('diagnostics', []))
    lines.extend(str(item) for item in report.get('runtime_diagnostics', []))
    return '\n'.join(lines) + '\n'


def observe(commit, mode, directory, *, fetch=ssh_report, sleep=time.sleep, monotonic=time.monotonic,
            timeout=1800):
    deadline = monotonic() + timeout
    last_state = None
    last_report = None
    while monotonic() < deadline:
        try:
            report = fetch(directory, commit, mode)
            last_report = report
            phase = report['state'] + '/' + report.get('deployment', {}).get('state', 'waiting')
            if phase != last_state:
                print('Server observation: ' + phase, flush=True)
                last_state = phase
            if report['state'] not in {'pending', 'error'}:
                return report
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            if last_state != 'unavailable':
                print('Waiting for the restricted diagnostics endpoint.', flush=True)
                last_state = 'unavailable'
        sleep(20)
    return {'format': 1, 'commit': commit, 'state': 'error',
            'deployment': {**((last_report or {}).get('deployment', {})),
                           'reason': 'Observation timed out; deployment success was not confirmed.'}}


def main():
    os.umask(0o077)
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    commit, ci = target(event, os.environ['GITHUB_EVENT_NAME'], os.environ['GITHUB_SHA'])
    mode = event.get('inputs', {}).get('mode', 'observe')
    if mode not in {'observe', 'self-test'}:
        raise ValueError('Invalid observation mode')
    environment = 'diagnostics-test' if mode == 'self-test' else 'production'
    run_url = 'https://github.com/' + REPOSITORY + '/actions/runs/' + os.environ['GITHUB_RUN_ID']
    directory = Path('deployment-feedback')
    directory.mkdir(exist_ok=True)
    deployment_id = None
    report = {'format': 1, 'commit': commit, 'state': 'error',
              'deployment': {'reason': 'Observer could not complete; inspect workflow configuration.'}}
    try:
        deployment = api('/deployments', {'ref': commit, 'auto_merge': False, 'required_contexts': [],
                         'environment': environment, 'production_environment': mode != 'self-test',
                         'transient_environment': mode == 'self-test',
                         'description': 'Isolated diagnostics test' if mode == 'self-test' else 'Server deployment observation'})
        deployment_id = deployment['id']
        api(f'/deployments/{deployment_id}/statuses', {'state': 'in_progress', 'log_url': run_url,
            'description': 'Waiting for verified server result', 'auto_inactive': False})
        if ci != 'success':
            report = {'format': 1, 'commit': commit, 'state': 'failure',
                      'deployment': {'failed_phase': 'ci', 'reason': 'CI did not pass; automatic deployment is blocked.'}}
        else:
            with tempfile.TemporaryDirectory(prefix='feedback-ssh-') as temporary:
                secret_dir = Path(temporary)
                for name, variable in [('key', 'DEPLOY_FEEDBACK_KEY'), ('known_hosts', 'DEPLOY_FEEDBACK_KNOWN_HOSTS')]:
                    path = secret_dir / name
                    path.write_text(os.environ[variable].strip() + '\n')
                    path.chmod(0o600)
                report = observe(commit, mode, secret_dir)
    except Exception:
        print('Deployment observation failed; no host secrets or raw exception details are published.')
    finally:
        (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        log = render_log(report)
        (directory / 'diagnostics.log').write_text(log, encoding='utf-8')
        # Prefix every line so server text cannot inject Actions workflow commands.
        print('\n'.join('  ' + line for line in log.splitlines()), flush=True)
        summary = '# Deployment feedback\n\n' + '\n'.join('    ' + line for line in log.splitlines()) + '\n'
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
            stream.write(summary)
        if deployment_id:
            state = {'success': 'success', 'failure': 'failure', 'error': 'error', 'superseded': 'inactive'}[report['state']]
            try:
                api(f'/deployments/{deployment_id}/statuses', {'state': state, 'log_url': run_url,
                    'description': ('Isolated test: expected build failure' if mode == 'self-test' else
                                    'Server result: ' + report['state']), 'auto_inactive': mode != 'self-test'})
            except Exception:
                print('Could not publish final deployment status; the workflow report remains available.')
                return 1
    return 0 if report['state'] in {'success', 'superseded'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
