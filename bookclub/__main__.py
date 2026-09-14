"""Offline config validation. Never imports the bot, reads .env, or uses Discord."""
import argparse
from pathlib import Path
import tempfile

from .config import load_config
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Validate book club configuration locally; no Discord connection.')
    parser.add_argument('--check-config', required=True, metavar='FILE')
    args = parser.parse_args()
    configuration = load_config(args.check_config)
    with tempfile.TemporaryDirectory(prefix='bookclub-check-') as directory:
        store = Store(Path(directory) / 'check.sqlite3')
        for guild_id, values in configuration.items():
            store.configure(guild_id, values)
    print(f'Configuration syntax OK: {len(configuration)} guild(s). Channel existence and permissions were NOT checked.')


if __name__ == '__main__':
    main()
