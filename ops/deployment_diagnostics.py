"""Private command output and an allowlisted diagnostic log safe for public GitHub."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import time
import uuid


PHASES = {'preflight', 'building', 'backup', 'deploying', 'updated', 'unchanged', 'failed', 'rollback'}
RECOVERY = {'original-container-restarted', 'manual-schema-change', 'manual-recovery-required',
            'previous-image-healthy-live-data', 'previous-image-running-manual-health-check'}
SHA = re.compile(r'[a-f0-9]{40}')


def public_reason(value: str) -> str:
    # Only exact string literals in our own source are publishable. Never publish
    # arbitrary exception text, subprocess arguments, environment, or DB values.
    tree = ast.parse(Path(__file__).with_name('update_bot.py').read_text(encoding='utf-8'))
    allowed = {node.args[0].value for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'UpdateError' and node.args
               and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    if value in allowed or re.fullmatch(r'Command (?:failed|unavailable or timed out): (?:docker|git)', value):
        return value
    return 'Deployment rejected; see phase, command exit status and diagnostic log.'


def extract_diagnostics(output: str, repo: Path) -> list[str]:
    """Extract fixed error categories and source locations; never redact-and-publish raw text."""
    results = []
    categories = {
        'no space left on device': 'Disk is full.',
        'permission denied': 'Permission denied.',
        'temporary failure in name resolution': 'DNS resolution failed.',
        'connection refused': 'Connection refused.',
        'connection timed out': 'Network connection timed out.',
        'no matching distribution found': 'Python dependency/version was not found.',
        'resolutionimpossible': 'Python dependencies conflict.',
        'failed to calculate checksum': 'Docker could not read a file from the build context.',
        'failed to compute cache key': 'Docker build context/cache step failed.',
        'out of memory': 'Process ran out of memory.',
        'modulenotfounderror': 'Python module is missing.',
        'syntaxerror': 'Python syntax error.',
        'importerror': 'Python import failed.',
        'traceback (most recent call last)': 'Python traceback follows (source locations only).',
    }
    lower = output.lower()
    results.extend(message for needle, message in categories.items() if needle in lower)
    for value in re.findall(r'(?:exit code:|returned a non-zero code:)\s*(\d{1,3})\b', output):
        results.append('Build process exit code: ' + value)
    for value in re.findall(r'\bDockerfile:(\d{1,6})\b', output):
        results.append('Dockerfile line ' + value)
    # Only paths that exist in the public application checkout may be disclosed.
    for path, line in re.findall(r'File "(/app/[A-Za-z0-9_./-]+\.py)", line (\d{1,6})', output):
        relative = Path(path.removeprefix('/app/'))
        if '..' not in relative.parts and (repo / relative).is_file():
            results.append('Source: ' + relative.as_posix() + ':' + line)
    return list(dict.fromkeys(results))[:40]


class DeploymentDiagnostics:
    def __init__(self, state: Path, repo: Path):
        self.identifier = uuid.uuid4().hex
        self.directory = state / 'logs' / self.identifier
        self.directory.mkdir(parents=True, mode=0o700)
        os.chmod(self.directory.parent, 0o700)
        self.repo = repo
        self.entries = []
        self.raw_size = 0

    def record(self, args, code, stdout='', stderr='', *, timed_out=False):
        tool = args[0] if args[0] in {'git', 'docker'} else 'process'
        entry = {'time': int(time.time()), 'tool': tool, 'exit_code': code, 'timeout': timed_out}
        capture = args[:2] in (['docker', 'logs'], ['docker', 'build']) or (
            args[:2] == ['docker', 'compose'] and 'build' in args)
        if capture:
            def decode(value):
                return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
            output = decode(stdout) + '\n' + decode(stderr)
            entry['diagnostics'] = extract_diagnostics(output, self.repo)
            # Raw build/runtime output stays on the host, bounded, root-only.
            if self.raw_size < 2_000_000:
                raw = json.dumps({**entry, 'output': output[-131072:]}, ensure_ascii=False) + '\n'
                path = self.directory / 'private-output.jsonl'
                try:
                    with path.open('a', encoding='utf-8') as stream:
                        os.chmod(path, 0o600)
                        stream.write(raw)
                    self.raw_size += len(raw.encode('utf-8'))
                except OSError:
                    entry['output_unavailable'] = True
        self.entries.append(entry)
        self.save()

    def phase(self, value):
        self.entries.append({'time': int(time.time()), 'phase': value if value in PHASES else 'preflight'})
        self.save()

    def save(self):
        # Local import avoids a circular dependency in script and package modes.
        try:
            from .update_bot import private_json
        except ImportError:
            from update_bot import private_json
        try:
            private_json(self.directory / 'public.json', {'entries': self.entries[-500:]})
        except OSError:
            # Logging must never prevent rollback when the disk is full.
            pass
