#!/usr/bin/env python3
"""Trigger DeShuffle's existing health workflow when its checks go stale."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


API = 'https://api.github.com/repos/delta-dashed/DeShuffle'
WORKFLOW = '/actions/workflows/bot-health.yml'
# Leave room for runner startup and timer jitter near the five-minute boundary.
FRESH_SECONDS = 240
ACTIVE_SECONDS = 600


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def github(token, method, path, body=None):
    request = Request(API + path, method=method, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
        'User-Agent': 'DeShuffle-independent-health-timer',
    }, data=json.dumps(body).encode() if body is not None else None)
    try:
        with build_opener(NoRedirects).open(request, timeout=25) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('GitHub response too large')
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        # Do not log the response, request headers, or token.
        raise RuntimeError('GitHub HTTP status ' + str(error.code)) from None


def decision(runs, now):
    for run in runs:
        if run.get('head_branch') != 'main':
            continue
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00')).timestamp()
        age = now - created
        if age < -60:
            raise ValueError('Clock differs from GitHub')
        if run.get('status') in {'queued', 'in_progress', 'waiting', 'pending', 'requested'}:
            if age < ACTIVE_SECONDS:
                return 'active-check'
        elif run.get('status') == 'completed' and run.get('conclusion') in {'success', 'failure'}:
            if age < FRESH_SECONDS:
                return 'recent-check'
    return 'dispatch'


def tick(token, *, request=github, now=time.time):
    payload = request(token, 'GET', WORKFLOW + '/runs?branch=main&per_page=10')
    runs = payload['workflow_runs']
    if not isinstance(runs, list):
        raise ValueError('Invalid GitHub response')
    result = decision(runs, now())
    if result == 'dispatch':
        request(token, 'POST', WORKFLOW + '/dispatches',
                {'ref': 'main', 'inputs': {'trigger_source': 'external-timer'}})
    return result


def main():
    try:
        credential = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'github-token'
        token = credential.read_text(encoding='utf-8').strip()
        if not token.startswith('github_pat_') or any(character.isspace() for character in token):
            raise ValueError('A repository-scoped fine-grained token is required')
        result = tick(token)
        print(datetime.now(timezone.utc).isoformat() + ' health-timer: ' + result, flush=True)
        return 0
    except Exception as error:
        # Exception text may contain private request details. Only the type is safe.
        print('health-timer: failed (' + type(error).__name__ + '); check service access and credential expiry',
              file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
