"""Host-controlled, startup-only switches for the temporary archive importer."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

from .store import ClubError


@dataclass(frozen=True)
class ImportConfig:
    enabled: bool = False
    allowed_user_ids: tuple[int, ...] = ()
    allowed_guild_ids: tuple[int, ...] = ()
    allowed_channel_ids: tuple[int, ...] = ()
    budget_id: str = 'legacy-essays-v1'
    max_runs: int = 1
    max_accounted_tokens: int = 100_000
    max_messages: int = 200
    max_threads: int = 30
    max_input_bytes: int = 48_000
    max_attachment_bytes: int = 20_000_000
    timeout_seconds: int = 180
    executable: str = 'codex'
    codex_home: str | None = None
    model: str | None = None


def load_import_config(path=None):
    path = path or os.getenv('BOOKCLUB_IMPORT_CONFIG_FILE')
    if not path:
        return ImportConfig()
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise ClubError('Не удалось прочитать конфигурацию временного импорта.') from exc
    if not isinstance(data, dict) or set(data) - set(ImportConfig.__dataclass_fields__):
        raise ClubError('Неизвестные поля конфигурации временного импорта.')
    if type(data.get('enabled', False)) is not bool:
        raise ClubError('import.enabled должен быть true или false.')
    for key in ('allowed_user_ids', 'allowed_guild_ids', 'allowed_channel_ids'):
        values = data.get(key, [])
        if not isinstance(values, list) or any(type(v) is not int or not 0 < v < 2**63 for v in values):
            raise ClubError(f'import.{key}: нужен список числовых ID.')
        data[key] = tuple(dict.fromkeys(values))
    bounds = dict(max_runs=(1, 100), max_accounted_tokens=(1, 10_000_000), max_messages=(1, 1000),
                  max_threads=(1, 100), max_input_bytes=(2000, 200_000),
                  max_attachment_bytes=(0, 100_000_000), timeout_seconds=(30, 600))
    for key, (low, high) in bounds.items():
        value = data.get(key, getattr(ImportConfig(), key))
        if type(value) is not int or not low <= value <= high:
            raise ClubError(f'import.{key}: целое число от {low} до {high}.')
    for key in ('budget_id', 'executable', 'codex_home', 'model'):
        value = data.get(key, getattr(ImportConfig(), key))
        if value is None and key in ('codex_home', 'model'):
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(c in value for c in '\r\n\0'):
            raise ClubError(f'import.{key}: некорректная строка.')
    config = ImportConfig(**data)
    if config.enabled and not all((config.allowed_user_ids, config.allowed_guild_ids, config.allowed_channel_ids)):
        raise ClubError('Для включения импорта задайте разрешённых пользователей, серверы и исходные каналы.')
    return config
